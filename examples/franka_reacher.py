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
    vis_ee_target = True
    robot_file = args.robot + ".yml"
    task_file = args.robot + "_reacher.yml"
    world_file = "collision_primitives_3d.yml"

    gym = gym_instance.gym
    sim = gym_instance.sim
    world_yml = join_path(get_gym_configs_path(), world_file)
    with open(world_yml) as file:
        world_params = yaml.load(file, Loader=yaml.FullLoader)

    robot_yml = join_path(get_gym_configs_path(), args.robot + ".yml")
    with open(robot_yml) as file:
        robot_params = yaml.load(file, Loader=yaml.FullLoader)
    sim_params = robot_params["sim_params"]
    sim_params["asset_root"] = get_assets_path()
    if args.cuda:
        device = "cuda"
    else:
        device = "cpu"

    sim_params["collision_model"] = None

    # create robot simulation:
    robot_sim = RobotSim(gym_instance=gym, sim_instance=sim, **sim_params, device=device)

    # create gym environment:
    robot_pose = sim_params["robot_pose"]
    env_ptr = gym_instance.env_list[0]
    robot_ptr = robot_sim.spawn_robot(env_ptr, robot_pose, coll_id=2)

    device = torch.device("cuda", 0)

    tensor_args = {"device": device, "dtype": torch.float32}

    # get pose
    w_T_r = copy.deepcopy(robot_sim.spawn_robot_pose)

    w_T_robot = torch.eye(4)
    quat = torch.tensor([w_T_r.r.w, w_T_r.r.x, w_T_r.r.y, w_T_r.r.z]).unsqueeze(0)
    rot = quaternion_to_matrix(quat)
    w_T_robot[0, 3] = w_T_r.p.x
    w_T_robot[1, 3] = w_T_r.p.y
    w_T_robot[2, 3] = w_T_r.p.z
    w_T_robot[:3, :3] = rot[0]

    world_instance = World(gym, sim, env_ptr, world_params, w_T_r=w_T_r)

    # create MPC controller
    mpc_control = ReacherTask(task_file, robot_file, world_file, tensor_args)

    # mpc_control.update_params(goal_state=x_des)
    mpc_control.update_params(goal_ee_pos=[0.55, 0, 0.61], goal_ee_quat=[0.0, 0.99, -0.01, -0.01])

    if vis_ee_target:
        # spawn object:
        x, y, z = 0.0, 0.0, 0.0
        tray_color = gymapi.Vec3(0.8, 0.1, 0.1)
        asset_options = gymapi.AssetOptions()
        asset_options.armature = 0.001
        asset_options.fix_base_link = True
        asset_options.thickness = 0.002

        object_pose = gymapi.Transform()
        object_pose.p = gymapi.Vec3(x, y, z)
        object_pose.r = gymapi.Quat(0, 0, 0, 1)

        # obj_asset_file = "urdf/mug/movable_mug.urdf"
        obj_asset_file = "urdf/mug/mug.urdf"
        obj_asset_root = get_assets_path()

        target_object = world_instance.spawn_object(
            obj_asset_file, obj_asset_root, object_pose, color=tray_color, name="ee_target_object"
        )
        target_base_handle = gym.get_actor_rigid_body_handle(env_ptr, target_object, 0)
        target_body_handle = gym.get_actor_rigid_body_handle(env_ptr, target_object, 6)
        gym.set_rigid_body_color(env_ptr, target_object, 0, gymapi.MESH_VISUAL_AND_COLLISION, tray_color)
        gym.set_rigid_body_color(env_ptr, target_object, 6, gymapi.MESH_VISUAL_AND_COLLISION, tray_color)

        obj_asset_file = "urdf/mug/mug.urdf"
        obj_asset_root = get_assets_path()

        ee_handle = world_instance.spawn_object(
            obj_asset_file, obj_asset_root, object_pose, color=tray_color, name="ee_current_as_mug"
        )
        ee_body_handle = gym.get_actor_rigid_body_handle(env_ptr, ee_handle, 0)
        tray_color = gymapi.Vec3(0.0, 0.8, 0.0)
        gym.set_rigid_body_color(env_ptr, ee_handle, 0, gymapi.MESH_VISUAL_AND_COLLISION, tray_color)

        g_pos = np.ravel(mpc_control.controller.rollout_fn.goal_ee_pos.cpu().numpy())
        g_q = np.ravel(mpc_control.controller.rollout_fn.goal_ee_quat.cpu().numpy())
        object_pose.p = gymapi.Vec3(g_pos[0], g_pos[1], g_pos[2])
        object_pose.r = gymapi.Quat(g_q[1], g_q[2], g_q[3], g_q[0])
        object_pose = w_T_r * object_pose
        gym.set_rigid_transform(env_ptr, target_base_handle, object_pose)

    w_robot_coord = CoordinateTransform(trans=w_T_robot[0:3, 3].unsqueeze(0), rot=w_T_robot[0:3, 0:3].unsqueeze(0))

    sim_dt = mpc_control.exp_params["control_dt"]

    q_des = None
    t_step = gym_instance.get_sim_time()

    g_pos = np.ravel(mpc_control.controller.rollout_fn.goal_ee_pos.cpu().numpy())
    g_q = np.ravel(mpc_control.controller.rollout_fn.goal_ee_quat.cpu().numpy())

    ee_pose = gymapi.Transform()
    i = 0
    while i > -100:

        try:
            # create a sinusoidally moving target
            target_pos = [0.55, 0.0, 0.40 - 0.25 * np.sin(0.005 * i)]
            target_quat = [0.0, 0.99, -0.01, -0.01]
            mpc_control.update_params(goal_ee_pos=target_pos, goal_ee_quat=target_quat)

            # move the red mug to the target position
            target_pose_gym_format = gymapi.Transform()
            target_pose_gym_format.p = gymapi.Vec3(target_pos[0], target_pos[1], target_pos[2])
            target_pose_gym_format.r = gymapi.Quat(target_quat[1], target_quat[2], target_quat[3], target_quat[0])
            target_pose_gym_format = copy.deepcopy(w_T_r) * copy.deepcopy(target_pose_gym_format)
            gym.set_rigid_transform(env_ptr, target_base_handle, copy.deepcopy(target_pose_gym_format))

            gym_instance.step()
            # if vis_ee_target:
            #     pose = copy.deepcopy(world_instance.get_pose(target_body_handle))

            #     # # >>>>> DEBUG
            #     # print(">>>>", target_pos, target_pose_gym_format.p, pose.p)
            #     # # <<<<< DEBUG

            #     pose = copy.deepcopy(w_T_r.inverse() * pose)

            #     if np.linalg.norm(g_pos - np.ravel([pose.p.x, pose.p.y, pose.p.z])) > 0.00001 or (
            #         np.linalg.norm(g_q - np.ravel([pose.r.w, pose.r.x, pose.r.y, pose.r.z])) > 0.0
            #     ):
            #         g_pos[0] = pose.p.x
            #         g_pos[1] = pose.p.y
            #         g_pos[2] = pose.p.z
            #         g_q[1] = pose.r.x
            #         g_q[2] = pose.r.y
            #         g_q[3] = pose.r.z
            #         g_q[0] = pose.r.w

            #         mpc_control.update_params(goal_ee_pos=g_pos, goal_ee_quat=g_q)
            t_step += sim_dt

            current_robot_state = copy.deepcopy(robot_sim.get_state(env_ptr, robot_ptr))

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
                gym.set_rigid_transform(env_ptr, ee_body_handle, copy.deepcopy(ee_pose))

            print(
                "time {:.3f} >>".format(gym_instance.get_sim_time()),
                ["{:.3f}".format(x) for x in ee_error],
                "{:.3f}".format(mpc_control.opt_dt),
                "{:.3f}".format(mpc_control.mpc_dt),
            )

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
