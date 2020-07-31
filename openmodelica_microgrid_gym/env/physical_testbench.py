import gym
import paramiko
import numpy as np
import matplotlib.pyplot as plt

from openmodelica_microgrid_gym.util import dq0_to_abc


class TestbenchEnv(gym.Env):

    viz_modes = {'episode', 'step', None}
    """Set of all valid visualisation modes"""

    def __init__(self, host: str = 'lea-jde10', username: str = 'root', password: str = '',
                 DT: float = 1/20000, executable_script_name: str = 'my_first_hps' ,num_steps: int = 1000,
                 kP: float = 0.01, kI: float = 5.0, i_ref: float = 10.0, f_nom: float = 50.0, i_limit: float = 30,
                 i_nominal: float = 20):

        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        self.host = host
        self.username = username
        self.password = password
        self.DT = DT
        self.executable_script_name = executable_script_name
        self.max_episode_steps = num_steps
        self.kP = kP
        self.kI = kI
        self.i_ref = i_ref
        self.f_nom = f_nom
        self.data = np.array(list())
        self.current_step = 0
        self.done = False
        self.i_limit = i_limit
        self.i_nominal = i_nominal

    @staticmethod
    def __decode_result(ssh_result):

        result = list()
        for line in ssh_result.read().splitlines():

            temp = line.decode("utf-8").split(",")

            if len(temp) == 11:
                temp.pop(-1)  # Drop the last item

                floats = [float(i) for i in temp]
                # print(floats)
                result.append(floats)
            elif len(temp) != 1:
                print(temp)

        N = (len(result))
        decoded_result = np.array(result)

        return decoded_result

    def rew_fun(self, Iabc_meas, Idq0_SP, phase) -> float:
        """
        Defines the reward function for the environment. Uses the observations and setpoints to evaluate the quality of the
        used parameters.
        Takes current measurement and setpoints so calculate the mean-root-error control error and uses a logarithmic
        barrier function in case of violating the current limit. Barrier function is adjustable using parameter mu.

        :param cols: list of variable names of the data
        :param data: observation data from the environment (ControlVariables, e.g. currents and voltages)
        :return: Error as negative reward
        """
        mu = 2

        # setpoints
        Iabc_SP = dq0_to_abc(Idq0_SP, phase)  # convert dq set-points into three-phase abc coordinates

        # control error = mean-root-error (MRE) of reference minus measurement
        # (due to normalization the control error is often around zero -> compared to MSE metric, the MRE provides
        #  better, i.e. more significant,  gradients)
        # plus barrier penalty for violating the current constraint
        error = np.sum((np.abs((Iabc_SP - Iabc_meas)) / self.i_limit) ** 0.5, axis=0) \
                + -np.sum(mu * np.log(1 - np.maximum(np.abs(Iabc_meas) - self.i_nominal, 0) / \
                (self.i_limit - self.i_nominal)), axis=0) * self.max_episode_steps

        return -error.squeeze()

    def reset(self, kP, kI):
        # toDo: ssh connection not open every episode!
        self.kP = kP
        self.kI = kI

        self.ssh.connect(self.host, username=self.username, password=self.password)


        #toDo: get SP and kP/I from agent?
        str_command = './{} {} {} {} {} {}'.format(self.executable_script_name, self.max_episode_steps, self.kP, self.kI,
                                                   self.i_ref, self.f_nom)
        ssh_stdin, ssh_stdout, ssh_stderr = self.ssh.exec_command(str_command)

        self.data = self.__decode_result(ssh_stdout)

        self.current_step = 0
        self.done = False

        self.ssh.close()

    def step(self):
        """
        Takes measured data and returns stepwise
        Measured data recorded in reset -> controller part of env
        """
        temp_data = self.data[self.current_step]
        self.current_step += 1

        I_abc_meas = temp_data[[3,4,5]]
        Idq0_SP = np.array([self.i_ref,0,0])
        phase = temp_data[9]

        reward = self.rew_fun(I_abc_meas, Idq0_SP, phase)

        if self.current_step == self.max_episode_steps:
            self.done = True

        info = []

        return temp_data, reward, self.done, info

    def render(self):

        N = (len(self.data))
        t = np.linspace(0, N * self.DT, N)

        V_A = self.data[:, 0]
        V_B = self.data[:, 1]
        V_C = self.data[:, 2]
        I_A = self.data[:, 3]
        I_B = self.data[:, 4]
        I_C = self.data[:, 5]
        I_D = self.data[:, 6]
        I_Q = self.data[:, 7]
        I_0 = self.data[:, 8]


        plt.plot(t, V_A, t, V_B, t, V_C)
        plt.ylabel('Voltages (V)')
        plt.show()

        plt.plot(t, I_A, t, I_B, t, I_C)
        plt.ylabel('Currents (A)')
        plt.show()

        plt.plot(t, I_D, t, I_Q, t, I_0)
        plt.ylabel('Currents DQ0(A)')
        plt.show()