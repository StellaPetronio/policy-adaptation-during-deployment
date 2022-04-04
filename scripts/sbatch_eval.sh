#!/bin/bash
#SBATCH --nodes 1
#SBATCH --ntasks 1
#SBATCH --cpus-per-task 1
#SBATCH --mem 48G
#SBATCH --time 02:00:00
#SBATCH --account vita
#SBATCH --gres gpu:1

python3 src/eval.py \
	--domain_name cartpole \
	--task_name swingup \
	--action_repeat 8 \
	--mode video12 \
	--use_inv \
	--num_shared_layers 8 \
	--seed 0 \
	--work_dir logs/cartpole_swingup/inv/0 \
	--pad_checkpoint 500k \
	--pad_num_episodes 50 \
	--threshold 3.0 \
	--dependent \
	--save_video \
	--episode_length 4000 \
	--video_speed 1 

