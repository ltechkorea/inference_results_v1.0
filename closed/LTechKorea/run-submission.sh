#!/bin/bash

set -e
#set -x

SUBMITTER="LTechKorea"

#TOPOLOGY="3d-unet bert dlrm rnnt resnet50 ssd-resnet34"
TOPOLOGY="3d-unet ssd-resnet34"

SCENARIO="Offline Server"
ACCURACY="default"

#make prebuild
if [ ! -e  "$(pwd)/build" ] ; then
  echo "Building pre-requisites..."
  make launch_docker DOCKER_COMMAND='make build'
fi

#
#make download_data BENCHMARKS=resnet50
#make launch_docker DOCKER_COMMAND='make preprocess_data BENCHMARKS=resnet50'

for topology in ${TOPOLOGY}
do
  SCENARIO="Offline,Server"
  ACCURACY="default"

  echo -e "\nStart for ${topology}"
  if [ "${topology}" == "3d-unet" ] ; then
    SCENARIO="Offline"
  fi
  if [ "${topology}" == "3d-unet" ] ||[ "${topology}" == "bert" ] \
    || [ "${topology}" == "dlrm" ] ; then
    ACCURACY="default,high_accuracy"
  fi

  RUN_ARGS="--benchmarks=${topology} --scenarios=${scenario} --config_ver=${ACCURACY}"
  RUN_ARGS="${RUN_ARGS} --verbose"

  for scenario in $(echo ${SCENARIO/','/ })
  do

    for accuracy in $(echo ${ACCURACY/','/ })
    do
      RUN_ARGS="--benchmarks=${topology} \
        --scenarios=${scenario} \
        --config_ver=${accuracy} \
        --test_mode=SubmissionRun \
        --verbose --fast"
      echo "[${topology}_${scenario}_${accuracy}] Generating Engines..."
      make launch_docker DOCKER_COMMAND="make generate_engines RUN_ARGS=\"${RUN_ARGS}\""

      echo "[${topology}_${scenario}_${accuracy}] Running Harness..."
      make launch_docker DOCKER_COMMAND="make run_harness RUN_ARGS=\"${RUN_ARGS}\""

      echo "[${topology}_${scenario}_${accuracy}] Update Results..."
      make update_results

      echo "[${topology}_${scenario}_${accuracy}] Running Compliance..."
      #make launch_docker DOCKER_COMMAND="make run_audit_harness RUN_ARGS=\"${RUN_ARGS}\""
      #make update_compliance
    done  # accuracy
  done  # scenario
done  # topology

#exit 0

# Truncate Accuracy Logs
echo "[ ${SUBMITTER} ] Truncate Accuracy Logs..."
make truncate_results SUBMITTER=${SUBMITTER}

# Subission Checker
echo "[ ${SUBMITTER} ] Submission Checking..."
make check_submission SUBMITTER=${SUBMITTER}

# Encrypting for Submission
echo "[ ${SUBMITTER} ] Encrypting and Packing..."
bash scripts/pack_submission.sh --pack

