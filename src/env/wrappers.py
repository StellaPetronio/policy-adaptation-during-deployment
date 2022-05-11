import numpy as np
import pandas as pd
from numpy.random import randint
from ast import literal_eval
import os
import gym
import torch
import torch.nn.functional as F
import torchvision.transforms.functional as TF
from torchvision.transforms import Grayscale, ColorJitter
from PIL import Image
import dmc2gym
from dm_control.suite import common
import cv2
from collections import deque
from utils import moving_average_reward, compute_distance


def make_pad_env(
        domain_name,
        task_name,
        seed=0,
        episode_length=1000,
        frame_stack=3,
        action_repeat=4,
        mode='train',
        dependent=False,
        threshold=0,
        window=3,
):
    """Make environment for PAD experiments"""
    env = dmc2gym.make(
        domain_name=domain_name,
        task_name=task_name,
        seed=seed,
        visualize_reward=False,
        from_pixels=True,
        height=100,
        width=100,
        episode_length=episode_length,
        frame_skip=action_repeat
    )
    env.seed(seed)
    env = GreenScreen(env, mode, threshold, dependent, window)
    env = FrameStack(env, frame_stack)
    env = ColorWrapper(env, mode, threshold, dependent, window)

    assert env.action_space.low.min() >= -1
    assert env.action_space.high.max() <= 1

    return env


class ColorWrapper(gym.Wrapper):
    """Wrapper for the color experiments"""

    def __init__(self, env, mode, threshold, dependent, window):
        assert isinstance(env, FrameStack), 'wrapped env must be a framestack'
        gym.Wrapper.__init__(self, env)
        self._max_episode_steps = env._max_episode_steps
        self._mode = mode
        self._threshold = threshold
        self._dependent = dependent
        self._window = window
        self._color = None
        self._change = 1
        self.time_step = 0
        if 'color' in self._mode:
            self._load_colors()

    def reset(self):
        self.time_step = 0
        if 'color' in self._mode:
            self.randomize()
        if 'video' in self._mode:
            # apply greenscreen
            self.reload_physics(
                {'skybox_rgb': [.2, .8, .2],
                 'skybox_rgb2': [.2, .8, .2],
                 'skybox_markrgb': [.2, .8, .2]
                 })
        self._change = 1
        return self.env.reset()

    def step(self, action, rewards=None):
        self.time_step += 1
        # Make a step
        next_obs, reward, done, info = self.env.step(action)
        rewards.append(reward)
        has_changed = False

        if self._dependent:
            avg_reward = moving_average_reward(rewards, current_ep=len(rewards) - 1, wind_lgth=self._window)

            if self.time_step % self._window == 0 and self.time_step > 1:
                self.modify_physics_model()
                has_changed = True

        return next_obs, reward, done, info, self._change, has_changed

    def randomize(self):
        if 'color' in self._mode :
            self.reload_physics(self.get_random_color())

    def _load_colors(self):
        assert self._mode in {'color_easy', 'color_hard'}
        self._colors = torch.load(f'src/env/data/{self._mode}.pt')

    def get_random_color(self):
        assert len(self._colors) >= 100, 'env must include at least 100 colors'
        return self._colors[randint(len(self._colors))]

    def reload_physics(self, setting_kwargs=None, state=None):
        domain_name = self._get_dmc_wrapper()._domain_name
        if setting_kwargs is None:
            setting_kwargs = {}
        if state is None:
            state = self._get_state()
        self._reload_physics(
            *common.settings.get_model_and_assets_from_setting_kwargs(
                domain_name + '.xml', setting_kwargs
            )
        )
        self._set_state(state)

    def modify_physics_model(self):
        _env = self._get_dmc_wrapper()
        #self._change *= -1
        #self._change = 0.2
        #self._change = self._change*10 if self._change < 1 else self._change / 10
        self._change = self._change - 0.3 if self._change >= 0.2 else 1
        #_env.physics.model.opt.gravity[:2] = self._change
        _env.physics.model.body_mass[1] = self._change

    def get_state(self):
        return self._get_state()

    def set_state(self, state):
        self._set_state(state)

    def _get_dmc_wrapper(self):
        _env = self.env
        while not isinstance(_env, dmc2gym.wrappers.DMCWrapper) and hasattr(_env, 'env'):
            _env = _env.env
        assert isinstance(_env, dmc2gym.wrappers.DMCWrapper), 'environment is not dmc2gym-wrapped'

        return _env

    def _reload_physics(self, xml_string, assets=None):
        _env = self.env
        while not hasattr(_env, '_physics') and hasattr(_env, 'env'):
            _env = _env.env
        assert hasattr(_env, '_physics'), 'environment does not have physics attribute'
        _env.physics.reload_from_xml_string(xml_string, assets=assets)

    def _get_physics(self):
        _env = self.env
        while not hasattr(_env, '_physics') and hasattr(_env, 'env'):
            _env = _env.env
        assert hasattr(_env, '_physics'), 'environment does not have physics attribute'

        return _env._physics

    def _get_state(self):
        return self._get_physics().get_state()

    def _set_state(self, state):
        self._get_physics().set_state(state)


class FrameStack(gym.Wrapper):
    """Stack frames as observation"""

    def __init__(self, env, k):
        gym.Wrapper.__init__(self, env)
        self._k = k
        self._frames = deque([], maxlen=k)
        shp = env.observation_space.shape
        self.observation_space = gym.spaces.Box(
            low=0,
            high=1,
            shape=((shp[0] * k,) + shp[1:]),
            dtype=env.observation_space.dtype
        )
        self._max_episode_steps = env._max_episode_steps

    def reset(self):
        obs = self.env.reset()
        for _ in range(self._k):
            self._frames.append(obs)
        return self._get_obs()

    def step(self, action, rewards=None):
        # Make a step
        obs, reward, done, info = self.env.step(action, rewards)
        self._frames.append(obs)
        return self._get_obs(), reward, done, info

    def _get_obs(self):
        assert len(self._frames) == self._k
        return np.concatenate(list(self._frames), axis=0)


def rgb_to_hsv(r, g, b):
    """Convert RGB color to HSV color"""
    maxc = max(r, g, b)
    minc = min(r, g, b)
    v = maxc
    if minc == maxc:
        return 0.0, 0.0, v
    s = (maxc - minc) / maxc
    rc = (maxc - r) / (maxc - minc)
    gc = (maxc - g) / (maxc - minc)
    bc = (maxc - b) / (maxc - minc)
    if r == maxc:
        h = bc - gc
    elif g == maxc:
        h = 2.0 + rc - bc
    else:
        h = 4.0 + gc - rc
    h = (h / 6.0) % 1.0
    return h, s, v


def do_green_screen(x, bg):
    """Removes green background from observation and replaces with bg; not optimized for speed"""
    assert isinstance(x, np.ndarray) and isinstance(bg, np.ndarray), 'inputs must be numpy arrays'
    assert x.dtype == np.uint8 and bg.dtype == np.uint8, 'inputs must be uint8 arrays'

    # Get image sizes
    x_h, x_w = x.shape[1:]

    # Convert to RGBA images
    im = TF.to_pil_image(torch.ByteTensor(x))
    im = im.convert('RGBA')
    pix = im.load()
    bg = TF.to_pil_image(torch.ByteTensor(bg))
    bg = bg.convert('RGBA')
    bg = bg.load()

    # Replace pixels
    for x in range(x_w):
        for y in range(x_h):
            r, g, b, a = pix[x, y]
            h_ratio, s_ratio, v_ratio = rgb_to_hsv(r / 255., g / 255., b / 255.)
            h, s, v = (h_ratio * 360, s_ratio * 255, v_ratio * 255)

            min_h, min_s, min_v = (100, 80, 70)
            max_h, max_s, max_v = (185, 255, 255)
            if min_h <= h <= max_h and min_s <= s <= max_s and min_v <= v <= max_v:
                pix[x, y] = bg[x, y]

    x = np.moveaxis(np.array(im).astype(np.uint8), -1, 0)[:3]

    return x


class GreenScreen(gym.Wrapper):
    """Green screen for video experiments"""

    def __init__(self, env, mode, threshold, dependent, window):
        gym.Wrapper.__init__(self, env)
        self._mode = mode
        self._threshold = threshold
        self._dependent = dependent
        self._window = window
        self._current_frame = 0  # When speed is left unchanged to 1, is equivalent to steps we take

        if 'video' in mode:
            self._video = mode
            if not self._video.endswith('.mp4'):
                self._video += '.mp4'
            self._video = os.path.join('src/env/data', self._video)
            self._data = self._load_video(self._video)
        else:
            self._video = None
        self._max_episode_steps = env._max_episode_steps

    def _load_video(self, video):
        """Load video from provided filepath and return as numpy array"""
        cap = cv2.VideoCapture(video)
        assert cap.get(cv2.CAP_PROP_FRAME_WIDTH) >= 100, 'width must be at least 100 pixels'
        assert cap.get(cv2.CAP_PROP_FRAME_HEIGHT) >= 100, 'height must be at least 100 pixels'
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        buf = np.empty((n, int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), 3),
                       np.dtype('uint8'))
        i, ret = 0, True
        while (i < n and ret):
            ret, frame = cap.read()
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            buf[i] = frame
            i += 1
        cap.release()
        return np.moveaxis(buf, -1, 1)

    def reset(self):
        self._current_frame = 0
        return self._greenscreen(self.env.reset())

    def step(self, action, rewards=None):
        obs, reward, done, info = self.env.step(action)
        self._current_frame += 1
        return self._greenscreen(obs), reward, done, info

    def _interpolate_bg(self, bg, size: tuple):
        """Interpolate background to size of observation"""
        bg = torch.from_numpy(bg).float().unsqueeze(0) / 255
        bg = F.interpolate(bg, size=size, mode='bilinear', align_corners=False)
        return (bg * 255).byte().squeeze(0).numpy()

    def _greenscreen(self, obs):
        """Applies greenscreen if video or steady mode is selected, otherwise does nothing"""
        if self._video:
            bg = self._data[self._current_frame % len(self._data)]  # select frame
            bg = self._interpolate_bg(bg, obs.shape[1:])  # scale bg to observation size
            return do_green_screen(obs, bg)  # apply greenscreen
        return obs

    def apply_to(self, obs):
        """Applies greenscreen mode of object to observation"""
        obs = obs.copy()
        channels_last = obs.shape[-1] == 3
        if channels_last:
            obs = torch.from_numpy(obs).permute(2, 0, 1).numpy()
        obs = self._greenscreen(obs)
        if channels_last:
            obs = torch.from_numpy(obs).permute(1, 2, 0).numpy()
        return obs
