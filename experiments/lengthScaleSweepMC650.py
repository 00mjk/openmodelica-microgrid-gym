#####################################
# Example using a FMU by OpenModelica and SafeOpt algorithm to find optimal controller parameters
# Simulation setup: Single inverter supplying 15 A d-current to an RL-load via a LC filter
# Controller: PI current controller gain parameters are optimized by SafeOpt


import logging
import os
import re
from distutils.util import strtobool
from functools import partial
# from time import strftime, gmtime
from itertools import product
from multiprocessing import Pool
from operator import itemgetter
from os.path import isfile
from typing import List
import seaborn as sns

import GPy
import gym
import numpy as np
import pandas as pd

import matplotlib.pyplot as plt
import matplotlib

from openmodelica_microgrid_gym.net import Network

params = {'backend': 'ps',
          'axes.labelsize': 8,  # fontsize for x and y labels (was 10)
          'axes.titlesize': 8,
          'font.size': 8,  # was 10
          'legend.fontsize': 8,  # was 10
          'xtick.labelsize': 8,
          'ytick.labelsize': 8,
          # 'text.usetex': True,
          # 'figure.figsize': [3.39, 2.5],
          'figure.figsize': [3.9, 3.1],
          'font.family': 'serif',
          'lines.linewidth': 1
          }
matplotlib.rcParams.update(params)

from openmodelica_microgrid_gym.agents import SafeOptAgent
from openmodelica_microgrid_gym.agents.util import MutableFloat, MutableParams
from openmodelica_microgrid_gym.aux_ctl import PI_params, MultiPhaseDQCurrentSourcingController
from openmodelica_microgrid_gym.env import PlotTmpl
from openmodelica_microgrid_gym.env.stochastic_components import Load, Noise
from openmodelica_microgrid_gym.execution.monte_carlo_runner import MonteCarloRunner
from openmodelica_microgrid_gym.util import dq0_to_abc, nested_map, FullHistory

# Choose which controller parameters should be adjusted by SafeOpt.
# - Kp: 1D example: Only the proportional gain Kp of the PI controller is adjusted
# - Ki: 1D example: Only the integral gain Ki of the PI controller is adjusted
# - Kpi: 2D example: Kp and Ki are adjusted simultaneously

adjust = 'Kpi'

# Check if really only one simulation scenario was selected
if adjust not in {'Kp', 'Ki', 'Kpi'}:
    raise ValueError("Please set 'adjust' to one of the following values: 'Kp', 'Ki', 'Kpi'")

include_simulate = True
show_plots = False
balanced_load = False
do_measurement = False

# If True: Results are stored to directory mentioned in: REBASE to DEV after MERGE #60!!
safe_results = True

# Files saves results and  resulting plots to the folder saves_VI_control_safeopt in the current directory
current_directory = os.getcwd()
save_folder = os.path.join(current_directory, r'len_sweep_cc_650_1')
# save_folder = os.path.join(current_directory, r'Paper_CC_meas')
# save_folder = os.path.join(current_directory, r'NotTurn21Back')
os.makedirs(save_folder, exist_ok=True)

lengthscale_vec_kP = 0.0005 * np.logspace(.5, 1.5, 5)
lengthscale_vec_kI = np.logspace(.5, 1.5, 5)

np.random.seed(0)

# Simulation definitions
net = Network.load('../net/net_single-inv-curr_Paper_SC.yaml')
delta_t = 1e-4  # simulation time step size / s
max_episode_steps = 1000  # number of simulation steps per episode
num_episodes = 100  # number of simulation episodes (i.e. SafeOpt iterations)
n_MC = 10  # number of Monte-Carlo samples for simulation - samples device parameters (e.g. L,R, noise) from
# distribution to represent real world more accurate
v_DC = 650 / 2  # DC-link voltage / V; will be set as model parameter in the FMU
nomFreq = 50  # nominal grid frequency / Hz
nomVoltPeak = 20  # 230 * 1.414  # nominal grid voltage / V
iLimit = 30  # inverter current limit / A
iNominal = 20  # nominal inverter current / A
mu = 2  # factor for barrier function (see below)
DroopGain = 0.0  # virtual droop gain for active power / W/Hz
QDroopGain = 0.0  # virtual droop gain for reactive power / VAR/V
i_ref = np.array([15, 0, 0])  # exemplary set point i.e. id = 15, iq = 0, i0 = 0 / A
# i_noise = 0.11 # Current measurement noise detected from testbench
# i_noise = np.array([[0.0, 0.0822], [0.0, 0.103], [0.0, 0.136]])

# Controller layout due to magnitude optimum:
L = 2.3e-3  # / mH
R = 400e-3  # 170e-3  # 585e-3  # / Ohm
# R = 585e-3#170e-3  # 585e-3  # / Ohm
tau_plant = L / R
gain_plant = 1 / R

# take inverter into account using s&h (exp(-s*delta_T/2))
Tn = tau_plant  # Due to compensate
Kp_init = tau_plant / (2 * delta_t * gain_plant * v_DC)
Ki_init = 33  # Kp_init / Tn


# Kp_init = 0.2
# Ki_init = 20

# Kp_init = 0


class Reward:
    def __init__(self):
        self._idx = None

    def set_idx(self, obs):
        if self._idx is None:
            self._idx = nested_map(
                lambda n: obs.index(n),
                [[f'lc.inductor{k}.i' for k in '123'], 'master.phase', [f'master.SPI{k}' for k in 'dq0']])

    def rew_fun(self, cols: List[str], data: np.ndarray) -> float:
        """
        Defines the reward function for the environment. Uses the observations and setpoints to evaluate the quality of the
        used parameters.
        Takes current measurement and setpoints so calculate the mean-root-error control error and uses a logarithmic
        barrier function in case of violating the current limit. Barrier function is adjustable using parameter mu.

        :param cols: list of variable names of the data
        :param data: observation data from the environment (ControlVariables, e.g. currents and voltages)
        :return: Error as negative reward
        """
        self.set_idx(cols)
        idx = self._idx

        Iabc_master = data[idx[0]]  # 3 phase currents at LC inductors
        phase = data[idx[1]]  # phase from the master controller needed for transformation

        # setpoints
        ISPdq0_master = data[idx[2]]  # setting dq reference
        ISPabc_master = dq0_to_abc(ISPdq0_master, phase)  # convert dq set-points into three-phase abc coordinates

        # control error = mean-root-error (MRE) of reference minus measurement
        # (due to normalization the control error is often around zero -> compared to MSE metric, the MRE provides
        #  better, i.e. more significant,  gradients)
        # plus barrier penalty for violating the current constraint
        error = np.sum((np.abs((ISPabc_master - Iabc_master)) / iLimit) ** 0.5, axis=0) \
                - np.sum(mu * np.log(1 - np.maximum(np.abs(Iabc_master) - iNominal, 0) / (iLimit - iNominal)), axis=0)
        error /= max_episode_steps

        return -error.squeeze()


def memoize(func):
    def wrapper(*args, **kwargs):
        len_kp, len_ki = args
        if not isfile(f'{save_folder}/{len_kp},{len_ki}.txt'):
            result = func(len_kp, len_ki)
            with open(f'{save_folder}/{len_kp},{len_ki}.txt', 'w')as f:
                print(f'({len_kp}, {len_ki}) {result}', file=f)
            return result
        with open(f'{save_folder}/{len_kp},{len_ki}.txt', 'r')as f:
            return bool(re.match(r'.*?\(.*?\) (\w*)', f.read()))

    return wrapper


# @memoize
def run_experiment(len_kp, len_ki):
    if isfile(f'{save_folder}/{len_kp:.4f},{len_ki:.4f}.txt'):
        with open(f'{save_folder}/{len_kp:.4f},{len_ki:.4f}.txt', 'r')as f:
            return strtobool(f.read().strip())

    #####################################
    # Definitions for the GP
    prior_mean = 0  # 2  # mean factor of the GP prior mean which is multiplied with the first performance of the initial set
    noise_var = 0.001  # 0.001 ** 2  # measurement noise sigma_omega
    prior_var = 2  # prior variance of the GP

    bounds = None
    lengthscale = [len_kp, len_ki]

    # 650 V
    bounds = [(0.001, 0.08), (0.001, 180)]

    # The performance should not drop below the safe threshold, which is defined by the factor safe_threshold times
    # the initial performance: safe_threshold = 0.8 means. Performance measurement for optimization are seen as
    # unsafe, if the new measured performance drops below 20 % of the initial performance of the initial safe (!)
    # parameter set
    safe_threshold = 0.75

    # The algorithm will not try to expand any points that are below this threshold. This makes the algorithm stop
    # expanding points eventually.
    # The following variable is multiplied with the first performance of the initial set by the factor below:
    explore_threshold = 0

    # Factor to multiply with the initial reward to give back an abort_reward-times higher negative reward in case of
    # limit exceeded
    # has to be negative due to normalized performance (regarding J_init = 1)
    abort_reward = -10

    # Definition of the kernel
    kernel = GPy.kern.Matern32(input_dim=len(bounds), variance=prior_var, lengthscale=lengthscale, ARD=True)

    #####################################
    # Definition of the controllers
    mutable_params = None
    current_dqp_iparams = None
    if adjust == 'Kp':
        # mutable_params = parameter (Kp gain of the current controller of the inverter) to be optimized using
        # the SafeOpt algorithm
        mutable_params = dict(currentP=MutableFloat(Kp_init))  # 5e-3))

        # Define the PI parameters for the current controller of the inverter
        current_dqp_iparams = PI_params(kP=mutable_params['currentP'], kI=Ki_init, limits=(-1, 1))

    # For 1D example, if Ki should be adjusted
    elif adjust == 'Ki':
        mutable_params = dict(currentI=MutableFloat(10))
        current_dqp_iparams = PI_params(kP=0.01, kI=mutable_params['currentI'], limits=(-1, 1))

    # For 2D example, choose Kp and Ki as mutable parameters
    elif adjust == 'Kpi':
        # mutable_params = dict(currentP=MutableFloat(Kp_init), currentI=MutableFloat(Ki_init))
        # mutable_params = dict(currentP=MutableFloat(0.38714), currentI=MutableFloat(52.5))
        # mutable_params = dict(currentP=MutableFloat(0.2), currentI=MutableFloat(33))

        # For vDC = 650 V
        mutable_params = dict(currentP=MutableFloat(0.004), currentI=MutableFloat(10))

        # mutable_params = dict(currentP=MutableFloat(0.037), currentI=MutableFloat(170))
        # mutable_params = dict(currentP=MutableFloat(0.07), currentI=MutableFloat(11))
        current_dqp_iparams = PI_params(kP=mutable_params['currentP'], kI=mutable_params['currentI'],
                                        limits=(-1, 1))

    # Define a current sourcing inverter as master inverter using the pi and droop parameters from above
    ctrl = MultiPhaseDQCurrentSourcingController(current_dqp_iparams, delta_t,
                                                 undersampling=1, name='master')

    i_ref_ = MutableParams([MutableFloat(f) for f in i_ref])

    #####################################
    # Definition of the optimization agent
    # The agent is using the SafeOpt algorithm by F. Berkenkamp (https://arxiv.org/abs/1509.01066) in this example
    # Arguments described above
    # History is used to store results
    agent = SafeOptAgent(mutable_params,
                         abort_reward,
                         kernel,
                         dict(bounds=bounds, noise_var=noise_var, prior_mean=prior_mean,
                              safe_threshold=safe_threshold, explore_threshold=explore_threshold),
                         [ctrl],
                         dict(master=[[f'lc.inductor{k}.i' for k in '123'], i_ref_]),
                         history=FullHistory()
                         )

    class PlotManager:
        def __init__(self, used_agent: SafeOptAgent, used_r_load: Load, used_l_load: Load, used_i_noise: Noise):
            self.agent = used_agent
            self.r_load = used_r_load
            self.l_load = used_l_load
            self.i_noise = used_i_noise

            # self.r_load.gains =  [elem *1e3 for elem in self.r_load.gains]
            # self.l_load.gains =  [elem *1e3 for elem in self.r_load.gains]

        # def set_title(self):
        # plt.title('Simulation: J = {:.2f}; R = {} \n L = {}; \n noise = {}'.format(self.agent.performance,
        #                                                                        ['%.4f' % elem for elem in
        #                                                                         self.r_load.gains],
        #                                                                        ['%.6f' % elem for elem in
        #                                                                         self.l_load.gains],
        #                                                                        ['%.4f' % elem for elem in
        #                                                                         self.i_noise.gains]))

        def save_abc(self, fig):
            if safe_results:
                fig.savefig(save_folder + '/J_{}_i_abc.pdf'.format(self.agent.performance))
                fig.savefig(save_folder + '/J_{}_i_abc.pgf'.format(self.agent.performance))

        def save_dq0(self, fig):
            if safe_results:
                fig.savefig(save_folder + '/J_{}_i_dq0.pdf'.format(self.agent.performance))
                fig.savefig(save_folder + '/J_{}_i_dq0.pgf'.format(self.agent.performance))

    #####################################
    # Definition of the environment using a FMU created by OpenModelica
    # (https://www.openmodelica.org/)
    # Using an inverter supplying a load
    # - using the reward function described above as callable in the env
    # - viz_cols used to choose which measurement values should be displayed (here, only the 3 currents across the
    #   inductors of the inverters are plotted. Labels and grid is adjusted using the PlotTmpl (For more information,
    #   see UserGuide)
    # - inputs to the models are the connection points to the inverters (see user guide for more details)
    # - model outputs are the the 3 currents through the inductors and the 3 voltages across the capacitors

    if include_simulate:

        # Defining unbalanced loads sampling from Gaussian distribution with sdt = 0.2*mean
        r_load = Load(R, 0.1 * R, balanced=balanced_load, tolerance=0.1)
        l_load = Load(L, 0.1 * L, balanced=balanced_load, tolerance=0.1)
        i_noise = Noise([0, 0, 0], [0.0023, 0.0015, 0.0018], 0.0005, 0.32)

        # r_load = Load(R, 0 * R, balanced=balanced_load)
        # l_load = Load(L, 0 * L, balanced=balanced_load)
        # i_noise = Noise([0, 0, 0], [0.0, 0.0, 0.0], 0.0, 0.0)

        def reset_loads():
            r_load.reset()
            l_load.reset()
            i_noise.reset()

        # plotter = PlotManager(agent, [r_load, l_load, i_noise])
        plotter = PlotManager(agent, r_load, l_load, i_noise)

        def xylables(fig):
            ax = fig.gca()
            ax.set_xlabel(r'$t\,/\,\mathrm{s}$')
            ax.set_ylabel('$i_{\mathrm{abc}}\,/\,\mathrm{A}$')
            ax.grid(which='both')
            # plt.legend(['Measurement', None , None, 'Setpoint', None, None], loc='best')
            plt.legend(ax.lines[::3], ('Measurement', 'Setpoint'), loc='best')
            # plt.legend(loc='best')
            # plotter.set_title()
            # plotter.save_abc(fig)
            # plt.title('Simulation')
            # time = strftime("%Y-%m-%d %H:%M:%S", gmtime())
            # if safe_results:
            #    fig.savefig(save_folder + '/abc_current' + time + '.pdf')
            # fig.savefig('Sim_vgl/abc_currentJ_{}_abcvoltage.pdf'.format())
            if show_plots:
                plt.show()
            else:
                plt.close(fig)

        def xylables_dq0(fig):
            ax = fig.gca()
            ax.set_xlabel(r'$t\,/\,\mathrm{s}$')
            ax.set_ylabel('$i_{\mathrm{dq0}}\,/\,\mathrm{A}$')
            ax.grid(which='both')
            # plotter.set_title()
            # plotter.save_dq0(fig)
            plt.ylim(0, 36)
            if show_plots:
                plt.show()
            else:
                plt.close(fig)

        def xylables_mdq0(fig):
            ax = fig.gca()
            ax.set_xlabel(r'$t\,/\,\mathrm{s}$')
            ax.set_ylabel('$m_{\mathrm{dq0}}\,/\,\mathrm{}$')
            plt.title('Simulation')
            ax.grid(which='both')
            # plt.ylim(0,36)
            if show_plots:
                plt.show()
            else:
                plt.close(fig)

        def xylables_mabc(fig):
            ax = fig.gca()
            ax.set_xlabel(r'$t\,/\,\mathrm{s}$')
            ax.set_ylabel('$m_{\mathrm{abc}}\,/\,\mathrm{}$')
            plt.title('Simulation')
            ax.grid(which='both')
            # plt.ylim(0,36)
            if show_plots:
                plt.show()
            else:
                plt.close(fig)

        def ugly_foo(t):

            if t >= .05:
                i_ref_[:] = np.array([20, 0, 0])
            else:

                i_ref_[:] = np.array([10, 0, 0])
            return partial(l_load.give_value, n=2)(t)

        env = gym.make('openmodelica_microgrid_gym:ModelicaEnv_test-v1',
                       reward_fun=Reward().rew_fun,
                       viz_cols=[
                           PlotTmpl([[f'lc.inductor{i}.i' for i in '123'], [f'master.SPI{i}' for i in 'abc']],
                                    callback=xylables,
                                    color=[['b', 'r', 'g'], ['b', 'r', 'g']],
                                    style=[[None], ['--']]
                                    ),
                           PlotTmpl([[f'master.CVI{i}' for i in 'dq0'], [f'master.SPI{i}' for i in 'dq0']],
                                    callback=xylables_dq0,
                                    color=[['b', 'r', 'g'], ['b', 'r', 'g']],
                                    style=[[None], ['--']]
                                    )
                       ],
                       # viz_cols = ['inverter1.*', 'rl.inductor1.i'],
                       log_level=logging.INFO,
                       viz_mode='episode',
                       max_episode_steps=max_episode_steps,
                       # model_params={'inverter1.gain.u': v_DC},
                       model_params={'lc.resistor1.R': partial(r_load.give_value, n=0),
                                     'lc.resistor2.R': partial(r_load.give_value, n=1),
                                     'lc.resistor3.R': partial(r_load.give_value, n=2),
                                     'lc.inductor1.L': partial(l_load.give_value, n=0),
                                     'lc.inductor2.L': partial(l_load.give_value, n=1),
                                     'lc.inductor3.L': ugly_foo},
                       model_path='../fmu/grid.paper.fmu',
                       # model_path='../omg_grid/omg_grid.Grids.Paper_SC.fmu',
                       net=net,
                       history=FullHistory(),
                       state_noise=i_noise,
                       action_time_delay=1
                       )

        runner = MonteCarloRunner(agent, env)
        runner.run(num_episodes, n_mc=n_MC, visualise=True, prepare_mc_experiment=reset_loads)

        with open(f'{save_folder}/{len_kp:.4f},{len_ki:.4f}.txt', 'w')as f:
            print(f'{agent.unsafe}', file=f)

        return agent.unsafe


if __name__ == '__main__':
    print(lengthscale_vec_kP, lengthscale_vec_kI)
    with Pool(5) as p:
        is_unsafe = p.starmap(run_experiment, product(lengthscale_vec_kP, lengthscale_vec_kI))

    safe_vec = np.empty([len(lengthscale_vec_kP), len(lengthscale_vec_kI)])

    for ((kk, ls_kP), (ii, ls_IP)), unsafe in zip(product(enumerate(lengthscale_vec_kP), enumerate(lengthscale_vec_kI)),
                                                  is_unsafe):
        safe_vec[kk, ii] = int(not unsafe)

    df = pd.DataFrame(safe_vec, index=[f'{i:.3f}' for i in lengthscale_vec_kP],
                      columns=[f'{i:.2f}' for i in lengthscale_vec_kI])
    df.to_pickle(save_folder + '/Unsafe_matrix')
    print(df)
    sns.heatmap(df)
    plt.show()

    # agent.unsafe = False
    #####################################
    # Performance results and parameters as well as plots are stored in folder pipi_signleInv
    # agent.history.df.to_csv('len_search/result.csv')
    # if safe_results:
    #   env.history.df.to_pickle('Simulation')

    print(safe_vec)
