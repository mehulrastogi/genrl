import numpy as np
import torch
import torch.nn as nn
import gym
from copy import deepcopy
import random

from jigglypuffRL.common import (
    ReplayBuffer,
    get_model,
    evaluate,
    save_params,
    load_params,
)


class TD3:
    """
    Twin Delayed DDPG
    Paper: https://arxiv.org/abs/1509.02971
    :param network_type: (str) The deep neural network layer types ['mlp']
    :param env: (Gym environment) The environment to learn from
    :param gamma: (float) discount factor
    :param replay_size: (int) Replay memory size
    :param batch_size: (int) Update batch size
    :param lr_p: (float) Policy network learning rate
    :param lr_q: (float) Q network learning rate
    :param polyak: (float) Polyak averaging weight to update target network
    :param policy_frequency: (int) Update actor and target networks every 
        policy_frequency steps
    :param epochs: (int) Number of epochs
    :param start_steps: (int) Number of exploratory steps at start
    :param steps_per_epoch: (int) Number of steps per epoch
    :param noise_std: (float) Standard deviation for action noise
    :param max_ep_len: (int) Maximum steps per episode
    :param start_update: (int) Number of steps before first parameter update
    :param update_interval: (int) Number of steps between parameter updates
    :param save_interval: (int) Number of steps between saves of models
    :param layers: (tuple or list) Number of neurons in hidden layers
    :param tensorboard_log: (str) the log location for tensorboard (if None,
        no logging)
    :param seed (int): seed for torch and gym
    :param render (boolean): if environment is to be rendered
    :param device (str): device to use for tensor operations; 'cpu' for cpu
        and 'cuda' for gpu
    :param seed: (int) seed for torch and gym
    :param render: (boolean) if environment is to be rendered
    :param device: (str) device to use for tensor operations; 'cpu' for cpu
        and 'cuda' for gpu
    :param pretrained: (boolean) if model has already been trained
    :param save_name: (str) model save name (if None, model hasn't been
        pretrained)
    :param save_version: (int) model save version (if None, model hasn't been
        pretrained)
    """
    def __init__(
        self,
        network_type,
        env,
        gamma=0.99,
        replay_size=1000000,
        batch_size=100,
        lr_p=0.001,
        lr_q=0.001,
        polyak=0.995,
        policy_frequency=2,
        epochs=100,
        start_steps=10000,
        steps_per_epoch=4000,
        noise_std=0.1,
        max_ep_len=1000,
        start_update=1000,
        update_interval=50,
        save_interval=5000,
        layers=(256, 256),
        tensorboard_log=None,
        seed=None,
        render=False,
        device="cpu",
        pretrained=False,
        save_name=None,
        save_version=None,
    ):

        self.network_type = network_type
        self.env = env
        self.gamma = gamma
        self.replay_size = replay_size
        self.batch_size = batch_size
        self.lr_p = lr_p
        self.lr_q = lr_q
        self.polyak = polyak
        self.policy_frequency = policy_frequency
        self.epochs = epochs
        self.start_steps = start_steps
        self.steps_per_epoch = steps_per_epoch
        self.noise_std = noise_std
        self.max_ep_len = max_ep_len
        self.start_update = start_update
        self.update_interval = update_interval
        self.save_interval = save_interval
        self.layers = layers
        self.tensorboard_log = tensorboard_log
        self.seed = seed
        self.render = render
        self.evaluate = evaluate
        self.pretrained = pretrained
        self.save_name = save_name
        self.save_version = save_version
        self.save = save_params
        self.load = load_params

        # Assign device
        if "cuda" in device and torch.cuda.is_available():
            self.device = torch.device(device)
        else:
            self.device = torch.device("cpu")

        # Assign seed
        if seed is not None:
            torch.manual_seed(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
            np.random.seed(seed)
            self.env.seed(seed)
            random.seed(seed)

        # Setup tensorboard writer
        self.writer = None
        if self.tensorboard_log is not None:
            from torch.utils.tensorboard import SummaryWriter

            self.writer = SummaryWriter(log_dir=self.tensorboard_log)

        self.create_model()

    def create_model(self):
        state_dim, action_dim, disc = self.get_env_properties()

        self.ac = get_model("ac", self.network_type)(
            state_dim, action_dim, self.layers, "Qsa", False
        ).to(self.device)

        self.ac.qf1 = self.ac.critic
        self.ac.qf2 = get_model("v", self.network_type)(
            state_dim, action_dim, hidden=self.layers, val_type="Qsa")

        if self.pretrained:
            self.checkpoint = self.load(self.save_name, self.save_version)
            self.ac.actor.load_state_dict(self.checkpoint["actor_weights"])
            self.ac.qf1.load_state_dict(self.checkpoint["critic_q1_weights"])
            self.ac.qf2.load_state_dict(self.checkpoint["critic_q2_weights"])
            for key, item in self.checkpoint.items():
                if "weights" not in key:
                    setattr(self, key, item)

        self.ac_target = deepcopy(self.ac).to(self.device)

        # freeze target network params
        for param in self.ac_target.parameters():
            param.requires_grad = False

        self.replay_buffer = ReplayBuffer(self.replay_size)
        self.q_params = list(self.ac.qf1.parameters()) + list(self.ac.qf2.parameters())
        self.optimizer_q = torch.optim.Adam(self.q_params, lr=self.lr_q)

        self.optimizer_policy = torch.optim.Adam(self.ac.actor.parameters(), lr=self.lr_p)

    def get_env_properties(self):
        state_dim = self.env.observation_space.shape[0]

        if isinstance(self.env.action_space, gym.spaces.Discrete):
            action_dim = self.env.action_space.n
            disc = True
        elif isinstance(self.env.action_space, gym.spaces.Box):
            action_dim = self.env.action_space.shape[0]
            disc = False
        else:
            raise NotImplementedError

        return state_dim, action_dim, disc

    def get_hyperparams(self):
        hyperparams = {
            "network_type": self.network_type,
            "gamma": self.gamma,
            "lr_p": self.lr_p,
            "lr_q": self.lr_q,
            "polyak": self.polyak,
            "policy_frequency": self.policy_frequency,
            "noise_std": self.noise_std,
            "critic_q1_weights": self.ac.qf1.state_dict(),
            "critic_q2_weights": self.ac.qf2.state_dict(),
            "actor_weights": self.ac.actor.state_dict
        }

        return hyperparams

    def select_action(self, state, deterministic=True):
        with torch.no_grad():
            action = self.ac_target.get_action(
                torch.as_tensor(state, dtype=torch.float32, device=self.device),
                deterministic=deterministic)[0].numpy()

        # add noise to output from policy network
        action += self.noise_std * np.random.randn(
            self.env.action_space.shape[0]
        )

        return np.clip(
            action,
            -self.env.action_space.high[0],
            self.env.action_space.high[0]
        )

    def get_q_loss(self, state, action, reward, next_state, done):
        q1 = self.ac.qf1.get_value(torch.cat([state, action], dim=-1))
        q2 = self.ac.qf2.get_value(torch.cat([state, action], dim=-1))

        with torch.no_grad():
            target_q1 = self.ac_target.qf1.get_value(torch.cat([next_state, self.ac_target.get_action(next_state, deterministic=True)[0]], dim=-1))
            target_q2 = self.ac_target.qf2.get_value(torch.cat([next_state, self.ac_target.get_action(next_state, deterministic=True)[0]], dim=-1))
            target_q = torch.min(target_q1, target_q2)

            target = reward + self.gamma * (1 - done) * target_q

        l1 = nn.MSELoss()(q1, target)
        l2 = nn.MSELoss()(q2, target)

        return l1 + l2

    def get_p_loss(self, state):
        q_pi = self.ac.get_value(torch.cat([
            state, self.ac.get_action(state, deterministic=True)[0]
        ], dim=-1))
        return -torch.mean(q_pi)

    def update_params(self, state, action, reward, next_state, done, timestep):
        self.optimizer_q.zero_grad()
        loss_q = self.get_q_loss(state, action, reward, next_state, done)
        loss_q.backward()
        self.optimizer_q.step()

        # Delayed Update
        if timestep % self.policy_frequency == 0:
            # freeze critic params for policy update
            for param in self.q_params:
                param.requires_grad = False

            self.optimizer_policy.zero_grad()
            loss_p = self.get_p_loss(state)
            loss_p.backward()
            self.optimizer_policy.step()

            # unfreeze critic params
            for param in self.ac.critic.parameters():
                param.requires_grad = True

            # update target network
            with torch.no_grad():
                for param, param_target in zip(
                    self.ac.parameters(), self.ac_target.parameters()
                ):
                    param_target.data.mul_(self.polyak)
                    param_target.data.add_((1 - self.polyak) * param.data)

    def learn(self):
        state, episode_reward, episode_len, episode = self.env.reset(), 0, 0, 0
        total_steps = self.steps_per_epoch * self.epochs

        for t in range(total_steps):
            # execute single transition
            if t > self.start_steps:
                action = self.select_action(state, deterministic=True)
            else:
                action = self.env.action_space.sample()

            next_state, reward, done, _ = self.env.step(action)
            if self.render:
                self.env.render()
            episode_reward += reward
            episode_len += 1

            # dont set d to True if max_ep_len reached
            done = False if episode_len == self.max_ep_len else done

            self.replay_buffer.push((state, action, reward, next_state, done))

            state = next_state

            if done or (episode_len == self.max_ep_len):
                if episode % 20 == 0:
                    print("Ep: {}, reward: {}, t: {}".format(
                        episode, episode_reward, t
                    ))
                if self.tensorboard_log:
                    self.writer.add_scalar("episode_reward", episode_reward, t)

                state, episode_reward, episode_len = self.env.reset(), 0, 0
                episode += 1

            # update params
            if t >= self.start_update and t % self.update_interval == 0:
                for _ in range(self.update_interval):
                    batch = self.replay_buffer.sample(
                        self.batch_size
                    )
                    states, actions, next_states, rewards, dones = (
                        x.to(self.device) for x in batch
                    )
                    self.update_params(
                        states, actions, next_states, rewards, dones, t
                    )

            if t >= self.start_update and t % self.save_interval == 0:
                self.checkpoint = self.get_hyperparams()
                if self.save_name is None:
                    self.save_name = self.network_type
                self.save_version = int(t / self.save_interval)
                self.save(self)

        self.env.close()
        if self.tensorboard_log:
            self.writer.close()


if __name__ == "__main__":
    env = gym.make("Pendulum-v0")
    algo = TD3("mlp", env, render=True)
    algo.learn()