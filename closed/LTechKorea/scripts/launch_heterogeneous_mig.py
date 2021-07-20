#! /usr/bin/env python3
# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''
This script facilitates running an individual MLPerf Inference benchmark on
a GPU istance while ensuring that other GPU instances on the same GPU are
simultaneously running other MLPerf Inference benchmarks in the background.

Given a set of benchmarks to run in the background and a benchmark to run
as the 'main benchmark', it will start each backround benchmark on a different
GPU instance and then monitor to ensure that all background benchmarks have entered
the timed phase of their execution. Once that is detected, it will launch the
main benchmark. Upon completion of the main benchmark, it checks to ensure that
the background benchmarks are still running and waits for them to finish. This
allows the user to confirm that measurements for the main benchmark are taken while
all background benchmarks are still running.
'''

import subprocess
import argparse
import sys
import os
import psutil
import time
import datetime
import select
import threading
import queue
import re


# TODO: should move this to a common module
datacenter_benchmarks = {
    'A100': {'3d-unet', 'bert', 'dlrm', 'rnnt', 'resnet50', 'ssd-resnet34'},
    'A30': {'bert', 'rnnt', 'resnet50', 'ssd-resnet34'},
}
edge_benchmarks = {
    'A100': {'3d-unet', 'bert', 'rnnt', 'resnet50', 'ssd-resnet34', 'ssd-mobilenet'},
    'A30': {'bert', 'rnnt', 'resnet50', 'ssd-resnet34', 'ssd-mobilenet'},
}
all_benchmarks = {
    'A100': datacenter_benchmarks['A100'].union(edge_benchmarks['A100']),
    'A30': datacenter_benchmarks['A30'].union(edge_benchmarks['A30']),
}
support_matrix = {
    'A100': {
        '3d-unet': {'offline', 'singlestream'},
        'bert': {'offline', 'server', 'singlestream'},
        'dlrm': {'offline', 'server'},
        'rnnt': {'offline', 'server', 'singlestream'},
        'resnet50': {'offline', 'server', 'singlestream', 'multistream'},
        'ssd-mobilenet': {'offline', 'singlestream', 'multistream'},
        'ssd-resnet34': {'offline', 'server', 'singlestream', 'multistream'},
    },
    'A30': {
        'bert': {'offline', 'server', 'singlestream'},
        'rnnt': {'offline', 'server', 'singlestream'},
        'resnet50': {'offline', 'server', 'singlestream', 'multistream'},
        'ssd-mobilenet': {'offline', 'singlestream', 'multistream'},
        'ssd-resnet34': {'offline', 'server', 'singlestream', 'multistream'},
    },
}

def get_gpu():
    # for the simplicity, we don't expect this script to be run on other than A30/A100
    p = subprocess.Popen("nvidia-smi --query-gpu=name --format=csv,noheader", universal_newlines=True, shell=True, stdout=subprocess.PIPE)
    # don't think heterogeneous GPUs are installed in the same system
    GPUs_found = [line.strip() for line in p.stdout]
    assert any(GPUs_found), "GPUs not found: {}".format(p.stdout)
    if 'A30' in GPUs_found[0]:
        return 'A30'
    elif 'A100' in GPUs_found[0]:
        return 'A100'
    else:
        return 'UNKNOWN'


def get_mig_uuids():
    p = subprocess.Popen("nvidia-smi --query-gpu=uuid --format=csv,noheader", universal_newlines=True, shell=True, stdout=subprocess.PIPE)
    GPUs_found = [line.strip() for line in p.stdout]
    p = subprocess.Popen("nvidia-smi -L | grep MIG | cut -d ' ' -f8 | sed 's/)//g'", universal_newlines=True, shell=True, stdout=subprocess.PIPE)
    MIGs_found = [line.strip() for line in p.stdout]
    mig_uuids = dict()
    for _g in GPUs_found:
        mig_uuids[_g] = [_m for _m in MIGs_found if _g.replace('GPU-', '') in _m]
    return mig_uuids


def get_target_mig_uuids():
    # Get the GPU with most number of MIG instances
    mig_uuids = get_mig_uuids()
    m = max(mig_uuids, key=lambda _k: len(mig_uuids[_k]))
    return mig_uuids[m]


def enqueue_output(out, queue):
    for line in iter(out.readline, b''):
        line = line.strip()
        if line:
            queue.put(line)
        time.sleep(.5)
    out.close()

def kill_process(process):
    try:
        for child in psutil.Process(process.pid).children(recursive=True):
            child.kill()
        process.kill()
    except psutil.NoSuchProcess:
        pass

def early_exit(subprocesses):
    print("Early exiting, killing all the background processes...")
    for process in subprocesses:
        kill_process(process)
    exit(1)

class Logger():
    def __init__(self):
        self.logfile = open('MIG_heterogeneous_{}.log'.format(datetime.datetime.now().strftime("%d_%H_%M_%S")), 'w')

    def log(self, string, loud=True):
        string += '\n'
        self.logfile.write(string)
        if loud:
            sys.stdout.write(string)

    def teardown(self):
        self.logfile.close()


def get_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--main_benchmark",
        default='resnet50',
        type=str.lower,
        help="The main benchmark from which to measure performance",
    )
    parser.add_argument(
        "--main_scenario",
        help="The scenario to run for the main benchmark",
        default='offline',
        type=str.lower,
        choices=['singlestream', 'multistream', 'offline', 'server'],
    )
    parser.add_argument(
        "--main_action",
        help="Makefile target for the main benchmark.",
        default="run_harness",
        choices=["run_harness", "run_audit_test01", "run_audit_test04", "run_audit_test05"]
    )
    parser.add_argument(
        "--main_benchmark_duration",
        type=int,
        default=600000,
        help="Duration for which to run the main benchmark in milliseconds",
    )
    parser.add_argument(
        "--main_benchmark_runargs",
        default="",
        type=str,
        help="Additional arguments to be passed in the harness RUN_ARGS for the main benchmark, passed as a string in quotes"
    )
    parser.add_argument(
        "--start_time_buffer",
        type=int,
        default=360000,
        help="Time to delay between launching the background workloads and launching the main benchmark (whether or not all background benchmarks have started) in milliseconds",
    )
    parser.add_argument(
        "--main_benchmark_immediate_start",
        default=True,
        type=bool,
        help="If all the background benchmarks have started, main benchmark starts immediately"
    )
    parser.add_argument(
        "--end_time_buffer_min",
        type=int,
        default=30000,
        help="Minimum acceptable time between completion of the main benchmark and completion of the background benchmarks in milliseconds",
    )
    parser.add_argument(
        "--background_benchmarks",
        help="The set of benchmarks to run in the background as a csv with no spaces (macros 'all', 'edge', and 'datacenter' are also supported)",
        default='all',
    )
    parser.add_argument(
        "--background_benchmark_duration",
        default='automatic',
        help="Duration for which to run the background benchmarks in milliseconds. If not specified, script automatically maintains background benchmarks until the main benchmark finishes.",
    )
    parser.add_argument(
        "--background_benchmark_timeout",
        default=7200000,
        help="Timeout of the background benchmark in automatic mode. Default to 120 minutes."
    )
    parser.add_argument(
        "--start_from_device",
        action='store_true',
        help="Start the dataset on the device for all benchmarks (main and background)",
    )
    parser.add_argument(
        "--lenient",
        action='store_true',
        help="Allow the run to proceed even if desired overlap of benchmarks is not acheived"
    )
    parser.add_argument(
        "--dryrun",
        action='store_true',
        help="Just print the benchmark commands that would be run",
    )
    parser.add_argument(
        "--verbose",
        action='store_true',
        help="More verbose output on stdout, especially from the background benchmarks",
    )
    return parser.parse_args()


def main():
    args = get_args()
    logger = Logger()

    GPU = get_gpu()
    assert GPU in support_matrix, "Unsupported GPU detected {}".format(GPU)
    
    # Expand the list of background benchmarks if macro was used and
    # then validate that the selected benchmarks are all supported.
    if args.background_benchmarks in {'all', 'datacenter', 'edge'}:
        background_benchmarks = list(
            {
                'datacenter': datacenter_benchmarks[GPU],
                'edge': edge_benchmarks[GPU],
                'all': all_benchmarks[GPU]
            }[args.background_benchmarks]
        )
    else:
        background_benchmarks = args.background_benchmarks.split(',')
    background_benchmarks.sort()  # Just for determinism :)
    for benchmark in background_benchmarks:
        assert benchmark in support_matrix[GPU], "Specified background benchmark {} is not supported".format(benchmark)

    # adjust background_benchmarks

    # Set the duration for which to run the background benchmark, if user provided any
    if args.background_benchmark_duration.isdigit():
        background_benchmark_duration = int(args.background_benchmark_duration)
    # If the background_benchmark_duration is automatic, we will let background benchmarks run until main benchmark finishes.
    # The timeout would be 60 minutes for now.
    elif args.background_benchmark_duration.lower() == 'automatic':
        logger.log("INFO: Setting the background benchmark to run for indefinitely long until main benchmark finishes.")
        background_benchmark_duration = args.background_benchmark_timeout
    else:
        assert false, "Unknown setting for background_benchmark_duration: {}".format(args.background_benchmark_duration)

    background_benchmark_action = "run_harness"

    # Set the prefix for logging messages that indicate something is wrong
    issue_prefix = "WARN: " if args.lenient else "ERROR: "

    # Ensure that the scenario chosen for the main benchmark is supported
    assert args.main_scenario in support_matrix[GPU][args.main_benchmark], "Specified main benchmark {} does not support the {} scenario".format(args.main_benchmark, args.main_scenario)

    # Get the value to select each GPU instance with CUDA_VISIBLE_DEVICES
    mig_uuids = get_target_mig_uuids()
    num_mig_instances = len(mig_uuids)

    # If we're running all the benchmarks at once, the main benchmark can't also be a background benchmark
    if args.background_benchmarks == 'all':
        background_benchmarks.remove(args.main_benchmark)

    # If we have more background_benchmarks than number of MIGs, choose from sorted name for reproducibility
    if len(background_benchmarks) >= num_mig_instances:
        background_benchmarks = background_benchmarks[:num_mig_instances-1]

    assert len(background_benchmarks) < num_mig_instances,\
         "Should have fewer background benchmarks than MIG instances to leave one for the main benchmark: main benchmark: {}, background benchmarks: {}, # MIGs: {}".format(args.main_benchmark, background_benchmarks, num_mig_instances)

    # Template command for running each of the benchmarks
    cmd_template = '''CUDA_VISIBLE_DEVICES={} make {} RUN_ARGS="--benchmarks={} --scenarios={} --config_ver=hetero --min_duration={}"'''
    if args.start_from_device:
        cmd_template = cmd_template[:-1] + ' --start_from_device=true"'
    main_cmd_template = cmd_template[:-1] + ' {} "'.format(args.main_benchmark_runargs)

    # Create directory to store logs from the background benchmarks so they do not contaminate
    # build/logs for submission
    background_log_dir = 'build/background_logs'
    if not (args.dryrun or os.path.exists(background_log_dir)):
        os.makedirs(background_log_dir)

    # Always set the min_query_count to 1 for background benchmarks so that their duration is purely based on the
    # min_duration setting
    background_cmd_template = cmd_template[:-1] + ' --min_query_count=1"' + ' LOG_DIR={}'.format(background_log_dir)

    # Launch each of the background workloads
    background_processes = []
    completed_benchmarks = [False for i in range(len(background_benchmarks))]
    for i in range(len(background_benchmarks)):
        cmd = background_cmd_template.format(
            mig_uuids[i + 1], background_benchmark_action, background_benchmarks[i], 'offline', background_benchmark_duration
        )
        logger.log('INFO: Launching background workload: {}'.format(cmd))
        if not args.dryrun:
            background_processes.append(subprocess.Popen(cmd, universal_newlines=True, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

    # Checking explicitly to detect that benchmarks have started by monitoring their logs in real-time
    # Using mechanism described on https://stackoverflow.com/questions/375427/a-non-blocking-read-on-a-subprocess-pipe-in-python to
    # poll stdout of each background benchmark
    if not args.dryrun:
        await_start_timeout = args.start_time_buffer / 1000
        logger.log("INFO: Waiting for background benchmarks to reach timed section with a {} second timeout".format(await_start_timeout))
        background_launch_time = datetime.datetime.now()
        background_queues = [queue.Queue() for i in range(len(background_benchmarks))]
        background_threads = [threading.Thread(target=enqueue_output, args=(background_processes[i].stdout, background_queues[i])) for i in range(len(background_benchmarks))]
        for t in background_threads:
            t.daemon = True
            t.start()
        unstarted_benchmarks = [True for i in range(len(background_benchmarks))]
        termination_check_counter = 0
        while ((datetime.datetime.now() - background_launch_time).seconds < await_start_timeout) and (unstarted_benchmarks.count(True) > 0):
            found_lines = False
            for i, benchmark in enumerate(background_benchmarks):
                if unstarted_benchmarks[i]:
                    try:
                        line = background_queues[i].get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        found_lines = True
                        line = line.strip()
                        logger.log("[{} {}]:".format(i, benchmark).upper() + line, loud=args.verbose)
                        if line.endswith("running actual test."):
                            logger.log("INFO: Detected that background benchmark {}, {} has started".format(i, background_benchmarks[i]))
                            unstarted_benchmarks[i] = False
            if not found_lines:
                termination_check_counter += 1
                if termination_check_counter == 30:
                    for i, benchmark in enumerate(background_benchmarks):
                        process_poll_result = background_processes[i].poll()
                        if not (process_poll_result is None):
                            unstarted_benchmarks[i] = False
                            completed_benchmarks[i] = True
                            logger.log(
                                issue_prefix +
                                "Background benchmark {}, {} exited earlier than expected with code {}".format(i, background_benchmarks[i], process_poll_result)
                            )
                            if (not args.lenient):
                                logger.teardown()
                                early_exit(background_processes)
                time.sleep(.1)

        early_starters = []
        if unstarted_benchmarks.count(True) > 0:
            early_starters = ["({}, {})".format(i, background_benchmarks[i]) for i in range(len(background_benchmarks)) if unstarted_benchmarks[i]]
            logger.log(issue_prefix + "Did not detect start of background benchmarks {} after waiting for specified delay.".format(early_starters))
            if (not args.lenient):
                logger.teardown()
                early_exit(background_processes)

        logger.log("INFO: All background benchmarks have started")
        remaining_delay = await_start_timeout - (datetime.datetime.now() - background_launch_time).seconds
        if not args.main_benchmark_immediate_start and remaining_delay > 0:
            logger.log("INFO: Waiting for remaining delay of {} seconds.".format(remaining_delay))
            time.sleep(remaining_delay)

    # Now launch the main benchmark
    cmd = main_cmd_template.format(
        mig_uuids[0],
        args.main_action,
        args.main_benchmark,
        args.main_scenario,
        args.main_benchmark_duration
    )
    logger.log("INFO: Launching main benchmark: {}".format(cmd))

    if not args.dryrun:
        main_benchmark_process = subprocess.Popen(cmd, universal_newlines=True, shell=True, stdout=sys.stdout, stderr=sys.stderr)
        main_exit_code = main_benchmark_process.wait()
        main_benchmark_complete_time = datetime.datetime.now()
        early_completions = completed_benchmarks.count(True)
        if main_exit_code != 0:
            logger.log(issue_prefix + "Main benchark ended with non-zero exit code {}.".format(main_exit_code))
            if (not args.lenient):
                logger.teardown()
                early_exit(background_processes)
        logger.log("INFO: Main benchmark ended with exit code {}.".format(main_exit_code))
        logger.log("INFO: Waiting for {} of {} background benchmarks to complete".format(len(background_benchmarks) - early_completions, len(background_benchmarks)))

        if args.background_benchmark_duration == "automatic":
            # Check if any of the background benchmarks exits with non-zero status
            # and stop all the background processes.
            for i, benchmark in enumerate(background_benchmarks):
                if not completed_benchmarks[i]:
                    poll_result = background_processes[i].poll()
                    if not (poll_result is None):
                        completed_benchmarks[i] = True
                        if poll_result != 0:
                            logger.log(issue_prefix + "Background benchmark {} completed with non-zero exit code {}.".format(benchmark, poll_result))
                            if (not args.lenient):
                                logger.teardown()
                                early_exit(background_processes)
            # Terminate all the processes
            logger.log("INFO: Automatically terminating all background benchmarks...")
            for i, process in enumerate(background_processes):
                logger.log("INFO: Terminating background process {}:{}.".format(i, process.pid, background_benchmarks[i]))
                kill_process(background_processes[i])
            # Tear down the logger and exit
            logger.teardown()
            exit(0)
        else:
            while completed_benchmarks.count(True) < len(background_benchmarks):
                for i, benchmark in enumerate(background_benchmarks):
                    if not completed_benchmarks[i]:
                        poll_result = background_processes[i].poll()
                        if not (poll_result is None):
                            if (datetime.datetime.now() - main_benchmark_complete_time).seconds < args.end_time_buffer_min / 1000:
                                early_completions += 1
                                logger.log(issue_prefix + "Background benchmark {} completed too early.".format(benchmark))
                            completed_benchmarks[i] = True
                            if poll_result != 0:
                                logger.log(issue_prefix + "Background benchmark {} completed with non-zero exit code {}.".format(benchmark, poll_result))
                                if (not args.lenient):
                                    logger.teardown()
                                    early_exit(background_processes)
                            logger.log("INFO: Background benchmark {}, {} completed with exit code {}.".format(i, benchmark, poll_result))

        # Drain the stdout for all background benchmarks
        for i, benchmark in enumerate(background_benchmarks):
            with background_queues[i].mutex:
                remaining_lines = list(background_queues[i].queue)
                for line in remaining_lines:
                    line = line.strip()
                    logger.log("[{}, {}]:".format(i, benchmark).upper() + line, loud=args.verbose)

        if len(early_starters) == 0:
            logger.log("INFO: All background benchmarks started before the benchmark under test")
        else:
            logger.log(
                issue_prefix +
                "Background benchmarks {} did not start long enough before the benchmark under test. Consider a longer start_time_buffer setting".format(early_starters)
            )

        if early_completions == 0:
            logger.log("INFO: All background benchmarks completed after the benchmark under test")
        else:
            logger.log(issue_prefix + "{} background benchmarks completed too early".format(early_completions))

    logger.teardown()


if __name__ == '__main__':
    main()
