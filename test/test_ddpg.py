import gym
import torch
import argparse
import numpy as np
from torch import nn
from torch.utils.tensorboard import SummaryWriter

from tianshou.policy import DDPGPolicy
from tianshou.trainer import step_trainer
from tianshou.data import Collector, ReplayBuffer
from tianshou.env import VectorEnv, SubprocVectorEnv


class Actor(nn.Module):
    def __init__(self, layer_num, state_shape, action_shape,
                 max_action, device='cpu'):
        super().__init__()
        self.device = device
        self.model = [
            nn.Linear(np.prod(state_shape), 128),
            nn.ReLU(inplace=True)]
        for i in range(layer_num):
            self.model += [nn.Linear(128, 128), nn.ReLU(inplace=True)]
        self.model += [nn.Linear(128, np.prod(action_shape))]
        self.model = nn.Sequential(*self.model)
        self._max = max_action

    def forward(self, s, **kwargs):
        s = torch.tensor(s, device=self.device, dtype=torch.float)
        batch = s.shape[0]
        s = s.view(batch, -1)
        logits = self.model(s)
        logits = self._max * torch.tanh(logits)
        return logits, None


class Critic(nn.Module):
    def __init__(self, layer_num, state_shape, action_shape, device='cpu'):
        super().__init__()
        self.device = device
        self.model = [
            nn.Linear(np.prod(state_shape) + np.prod(action_shape), 128),
            nn.ReLU(inplace=True)]
        for i in range(layer_num):
            self.model += [nn.Linear(128, 128), nn.ReLU(inplace=True)]
        self.model += [nn.Linear(128, 1)]
        self.model = nn.Sequential(*self.model)

    def forward(self, s, a):
        s = torch.tensor(s, device=self.device, dtype=torch.float)
        if isinstance(a, np.ndarray):
            a = torch.tensor(a, device=self.device, dtype=torch.float)
        batch = s.shape[0]
        s = s.view(batch, -1)
        a = a.view(batch, -1)
        logits = self.model(torch.cat([s, a], dim=1))
        return logits


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', type=str, default='Pendulum-v0')
    parser.add_argument('--seed', type=int, default=1626)
    parser.add_argument('--buffer-size', type=int, default=20000)
    parser.add_argument('--actor-lr', type=float, default=1e-4)
    parser.add_argument('--actor-wd', type=float, default=0)
    parser.add_argument('--critic-lr', type=float, default=1e-3)
    parser.add_argument('--critic-wd', type=float, default=1e-2)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--tau', type=float, default=0.005)
    parser.add_argument('--exploration-noise', type=float, default=0.1)
    parser.add_argument('--epoch', type=int, default=100)
    parser.add_argument('--step-per-epoch', type=int, default=2400)
    parser.add_argument('--collect-per-step', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=128)
    parser.add_argument('--layer-num', type=int, default=1)
    parser.add_argument('--training-num', type=int, default=1)
    parser.add_argument('--test-num', type=int, default=100)
    parser.add_argument('--logdir', type=str, default='log')
    parser.add_argument(
        '--device', type=str,
        default='cuda' if torch.cuda.is_available() else 'cpu')
    args = parser.parse_known_args()[0]
    return args


def test_ddpg(args=get_args()):
    env = gym.make(args.task)
    args.state_shape = env.observation_space.shape or env.observation_space.n
    args.action_shape = env.action_space.shape or env.action_space.n
    args.max_action = env.action_space.high[0]
    # train_envs = gym.make(args.task)
    train_envs = VectorEnv(
        [lambda: gym.make(args.task) for _ in range(args.training_num)],
        reset_after_done=True)
    # test_envs = gym.make(args.task)
    test_envs = SubprocVectorEnv(
        [lambda: gym.make(args.task) for _ in range(args.test_num)],
        reset_after_done=False)
    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    train_envs.seed(args.seed)
    test_envs.seed(args.seed)
    # model
    actor = Actor(
        args.layer_num, args.state_shape, args.action_shape,
        args.max_action, args.device
    ).to(args.device)
    actor_optim = torch.optim.Adam(
        actor.parameters(), lr=args.actor_lr, weight_decay=args.actor_wd)
    critic = Critic(
        args.layer_num, args.state_shape, args.action_shape, args.device
    ).to(args.device)
    critic_optim = torch.optim.Adam(
        critic.parameters(), lr=args.critic_lr, weight_decay=args.critic_wd)
    policy = DDPGPolicy(
        actor, actor_optim, critic, critic_optim,
        [env.action_space.low[0], env.action_space.high[0]],
        args.tau, args.gamma, args.exploration_noise)
    # collector
    train_collector = Collector(
        policy, train_envs, ReplayBuffer(args.buffer_size), 1)
    test_collector = Collector(policy, test_envs, stat_size=args.test_num)
    # log
    writer = SummaryWriter(args.logdir)

    def stop_fn(x):
        if args.task == 'Pendulum-v0':
            return x >= -250
        else:
            return False

    # trainer
    train_step, train_episode, test_step, test_episode, best_rew, duration = \
        step_trainer(
            policy, train_collector, test_collector, args.epoch,
            args.step_per_epoch, args.collect_per_step, args.test_num,
            args.batch_size, stop_fn=stop_fn, writer=writer)
    if args.task == 'Pendulum-v0':
        assert stop_fn(best_rew)
    train_collector.close()
    test_collector.close()
    if __name__ == '__main__':
        print(f'Collect {train_step} frame / {train_episode} episode during '
              f'training and {test_step} frame / {test_episode} episode during'
              f' test in {duration:.2f}s, best_reward: {best_rew}, speed: '
              f'{(train_step + test_step) / duration:.2f}it/s')
        # Let's watch its performance!
        env = gym.make(args.task)
        collector = Collector(policy, env)
        result = collector.collect(n_episode=1, render=1 / 35)
        print(f'Final reward: {result["rew"]}, length: {result["len"]}')
        collector.close()


if __name__ == '__main__':
    test_ddpg()