#
# MIT License
#
# Copyright (c) 2020-2021 NVIDIA CORPORATION.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.#
""" Example spawning a robot in gym

"""
import copy

from isaacgym import gymapi

pass  # <-- Alban: just here to force the importation of torch after isaacgym (isort tries to move the imports around)
import torch

torch.multiprocessing.set_start_method("spawn", force=True)
torch.set_num_threads(8)
torch.backends.cudnn.benchmark = False
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

import matplotlib

matplotlib.use("WebAgg")  # matplotlib.use('tkagg')

import argparse

import numpy as np
import yaml
from storm_kit.differentiable_robot_model.coordinate_transform import (
    CoordinateTransform,
    quaternion_to_matrix,
)
from storm_kit.gym.core import Gym, World
from storm_kit.gym.sim_robot import RobotSim
from storm_kit.mpc.task.reacher_task import ReacherTask
from storm_kit.util_file import (
    get_assets_path,
    get_gym_configs_path,
    join_path,
    load_yaml,
)

np.set_printoptions(precision=2)


def mpc_robot_interactive(args, gym_instance):

    # parameters
    vis_ee_target = True
    robot_file = args.robot + ".yml"
    task_file = args.robot + "_reacher.yml"
    world_file = "collision_primitives_3d.yml"

    # select gpu hardware
    device = torch.device("cuda", 0)

    # define tensor properties
    tensor_args = {"device": device, "dtype": torch.float32}

    # shortcuts to gym instance
    gym = gym_instance.gym
    sim = gym_instance.sim

    # environment parameters
    world_yml = join_path(get_gym_configs_path(), world_file)
    with open(world_yml) as file:
        world_params = yaml.load(file, Loader=yaml.FullLoader)

    # robot parameters
    robot_yml = join_path(get_gym_configs_path(), args.robot + ".yml")
    with open(robot_yml) as file:
        robot_params = yaml.load(file, Loader=yaml.FullLoader)
    sim_params = robot_params["sim_params"]
    sim_params["asset_root"] = get_assets_path()
    sim_params["collision_model"] = None

    # create robot simulation:
    robot_sim = RobotSim(gym_instance=gym, sim_instance=sim, **sim_params, device=device)

    # spawn robot in environment
    robot_pose = sim_params["robot_pose"]
    env_ptr = gym_instance.env_list[0]
    robot_ptr = robot_sim.spawn_robot(env_ptr, robot_pose, coll_id=2)

    # tranformation from world to eef of robot in spawning position
    w_T_r = copy.deepcopy(robot_sim.spawn_robot_pose)

    # same transformation in tensor form
    w_T_robot = torch.eye(4)
    quat = torch.tensor([w_T_r.r.w, w_T_r.r.x, w_T_r.r.y, w_T_r.r.z]).unsqueeze(0)
    rot = quaternion_to_matrix(quat)
    w_T_robot[0, 3] = w_T_r.p.x
    w_T_robot[1, 3] = w_T_r.p.y
    w_T_robot[2, 3] = w_T_r.p.z
    w_T_robot[:3, :3] = rot[0]

    # spawn obstacles in the environment #? ToCheck: they are defined with respect to the initial eef pose
    world_instance = World(gym, sim, env_ptr, world_params, w_T_r=w_T_r)

    # create MPC controller
    mpc_control = ReacherTask(task_file, robot_file, world_file, tensor_args)

    # mpc_control.update_params(goal_state=x_des)
    mpc_control.update_params(goal_ee_pos=[0.55, 0, 0.61], goal_ee_quat=[0.0, 0.99, -0.01, -0.01])

    if vis_ee_target:
        # spawn target mug
        x, y, z = 0.0, 0.0, 0.0
        tray_color = gymapi.Vec3(0.8, 0.1, 0.1)
        asset_options = gymapi.AssetOptions()
        asset_options.armature = 0.001
        asset_options.fix_base_link = True
        asset_options.thickness = 0.002

        object_pose = gymapi.Transform()
        object_pose.p = gymapi.Vec3(x, y, z)
        object_pose.r = gymapi.Quat(0, 0, 0, 1)

        obj_asset_file = "urdf/mug/mug.urdf"  # obj_asset_file = "urdf/mug/movable_mug.urdf"
        obj_asset_root = get_assets_path()

        target_object = world_instance.spawn_object(
            obj_asset_file, obj_asset_root, object_pose, color=tray_color, name="ee_target_object"
        )
        target_base_handle = gym.get_actor_rigid_body_handle(env_ptr, target_object, 0)
        # target_body_handle = gym.get_actor_rigid_body_handle(env_ptr, target_object, 6)
        gym.set_rigid_body_color(env_ptr, target_object, 0, gymapi.MESH_VISUAL_AND_COLLISION, tray_color)
        gym.set_rigid_body_color(env_ptr, target_object, 6, gymapi.MESH_VISUAL_AND_COLLISION, tray_color)

        # spawn mug held by the eef
        obj_asset_file = "urdf/mug/mug.urdf"
        obj_asset_root = get_assets_path()

        ee_handle = world_instance.spawn_object(
            obj_asset_file, obj_asset_root, object_pose, color=tray_color, name="ee_current_as_mug"
        )
        ee_base_handle = gym.get_actor_rigid_body_handle(env_ptr, ee_handle, 0)
        tray_color = gymapi.Vec3(0.0, 0.8, 0.0)
        gym.set_rigid_body_color(env_ptr, ee_handle, 0, gymapi.MESH_VISUAL_AND_COLLISION, tray_color)

        g_pos = np.ravel(mpc_control.controller.rollout_fn.goal_ee_pos.cpu().numpy())
        g_q = np.ravel(mpc_control.controller.rollout_fn.goal_ee_quat.cpu().numpy())
        object_pose.p = gymapi.Vec3(g_pos[0], g_pos[1], g_pos[2])
        object_pose.r = gymapi.Quat(g_q[1], g_q[2], g_q[3], g_q[0])
        object_pose = w_T_r * object_pose
        gym.set_rigid_transform(env_ptr, target_base_handle, object_pose)

    # create a class to easily tranform coordinates from one frame the another
    w_robot_coord = CoordinateTransform(trans=w_T_robot[0:3, 3].unsqueeze(0), rot=w_T_robot[0:3, 0:3].unsqueeze(0))

    # simulation timestep #! assumed to be equal to the MPC timestep
    sim_dt = mpc_control.exp_params["control_dt"]

    # initialize simulation step
    t_step = gym_instance.get_sim_time()

    # run the simu
    ee_pose = gymapi.Transform()
    i = 0
    while i > -100:

        try:
            # step simulation and time forward
            gym_instance.step()
            t_step += sim_dt
            current_time = gym_instance.get_sim_time()  # ? sim_time is only half t_step; why??

            # create a sinusoidally moving target (defined in the robot's base frame)
            period = 8
            amp = 0.25
            center = [0.55, 0.0, 0.4]
            n_planes = 8
            target_pos = [
                center[0]
                + np.sin(np.floor(current_time / period) * 2 * np.pi / n_planes)
                * 2
                * amp
                * np.sin(2 * np.pi / period * current_time),
                center[1]
                + np.cos(np.floor(current_time / period) * 2 * np.pi / n_planes)
                * 2
                * amp
                * np.sin(2 * np.pi / period * current_time),
                center[2] + amp * np.sin(2 * 2 * np.pi / period * current_time),
            ]
            target_quat = [0.0, 0.99, -0.01, -0.01]
            # set the new target for the mpc
            mpc_control.update_params(goal_ee_pos=target_pos, goal_ee_quat=target_quat)

            if vis_ee_target:
                # move the red mug to the target position for visualization (defined in the world frame)
                target_pose_gym_format = gymapi.Transform()
                target_pose_gym_format.p = gymapi.Vec3(target_pos[0], target_pos[1], target_pos[2])
                target_pose_gym_format.r = gymapi.Quat(target_quat[1], target_quat[2], target_quat[3], target_quat[0])
                target_pose_gym_format = copy.deepcopy(w_T_r) * copy.deepcopy(target_pose_gym_format)
                gym.set_rigid_transform(env_ptr, target_base_handle, copy.deepcopy(target_pose_gym_format))

            # get current robot state
            current_robot_state = copy.deepcopy(robot_sim.get_state(env_ptr, robot_ptr))

            # get next command from the MPC controller
            command = mpc_control.get_command(t_step, current_robot_state, control_dt=sim_dt, WAIT=True)

            filtered_state_mpc = current_robot_state  # mpc_control.current_state #! why that here?!
            curr_state = np.hstack(
                (filtered_state_mpc["position"], filtered_state_mpc["velocity"], filtered_state_mpc["acceleration"])
            )

            curr_state_tensor = torch.as_tensor(curr_state, **tensor_args).unsqueeze(0)
            # get position command:
            q_des = copy.deepcopy(command["position"])

            ee_error = mpc_control.get_current_error(filtered_state_mpc)

            pose_state = mpc_control.controller.rollout_fn.get_ee_pose(curr_state_tensor)

            # get current pose:
            e_pos = np.ravel(pose_state["ee_pos_seq"].cpu().numpy())
            e_quat = np.ravel(pose_state["ee_quat_seq"].cpu().numpy())
            ee_pose.p = copy.deepcopy(gymapi.Vec3(e_pos[0], e_pos[1], e_pos[2]))
            ee_pose.r = gymapi.Quat(e_quat[1], e_quat[2], e_quat[3], e_quat[0])

            ee_pose = copy.deepcopy(w_T_r) * copy.deepcopy(ee_pose)

            if vis_ee_target:
                gym.set_rigid_transform(env_ptr, ee_base_handle, copy.deepcopy(ee_pose))

            print(
                "time {:.3f} >>".format(current_time),
                ["{:.3f}".format(x) for x in ee_error],
                "{:.3f}".format(mpc_control.opt_dt),
                "{:.3f}".format(mpc_control.mpc_dt),
            )

            # display predicted trajectories
            gym_instance.clear_lines()
            top_trajs = mpc_control.top_trajs.cpu().float()
            n_p, n_t = top_trajs.shape[0], top_trajs.shape[1]
            w_pts = w_robot_coord.transform_point(top_trajs.view(n_p * n_t, 3)).view(n_p, n_t, 3)
            top_trajs = w_pts.cpu().numpy()
            color = np.array([0.0, 1.0, 0.0])
            for k in range(top_trajs.shape[0]):
                pts = top_trajs[k, :, :]
                color[0] = float(k) / float(top_trajs.shape[0])
                color[1] = 1.0 - float(k) / float(top_trajs.shape[0])
                gym_instance.draw_lines(pts, color=color)

            # send the command to the robot
            robot_sim.command_robot_position(q_des, env_ptr, robot_ptr)

            i += 1

        except KeyboardInterrupt:
            print("Closing")
            break
    mpc_control.close()
    return 1


if __name__ == "__main__":

    # instantiate empty gym:
    parser = argparse.ArgumentParser(description="pass args")
    parser.add_argument("--robot", type=str, default="franka", help="Robot to spawn")
    parser.add_argument("--cuda", action="store_true", default=True, help="use cuda")
    parser.add_argument("--headless", action="store_true", default=False, help="headless gym")
    parser.add_argument("--control_space", type=str, default="acc", help="Robot to spawn")
    args = parser.parse_args()

    sim_params = load_yaml(join_path(get_gym_configs_path(), "physx.yml"))
    sim_params["headless"] = args.headless
    gym_instance = Gym(**sim_params)

    mpc_robot_interactive(args, gym_instance)
