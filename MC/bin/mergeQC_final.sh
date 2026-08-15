#!/bin/bash

# Script to merge QC MC output
#
# This runs in the last stage of the LPM QC merging masterjob of an MC production.
# It merges the per-subjob QC files of one run into one file per QC task and then runs
# the QC finalization workflow on them, which applies the Checks and uploads the
# MonitorObjects and QualityObjects to the QCDB.
#
# $1 is the XML collection of the inputs. Those are either the QC archives written by
# the MC jobs themselves, or the ones written by an intermediate merging stage.

# make sure O2DPG + O2 is loaded
[ ! "${O2DPG_ROOT}" ] && echo "Error: This needs O2DPG loaded" && exit 1
[ ! "${O2_ROOT}" ] && echo "Error: This needs O2 loaded" && exit 1
[ ! "${QUALITYCONTROL_ROOT}" ] && echo "Error: This needs QualityControl loaded" && exit 1

inputFromIntermediateStage=$([ ! -z "$(sed -rn "s/.*turl=\"([^\"]*)\".*/\1/p" $1 | grep "Stage")" ] && echo 1 || echo 0)
if [ $inputFromIntermediateStage -eq 1 ]; then
  echo "Input files from intermediate MC QC merging stage"
else
  echo "Input files from main MC jobs"
fi

if [[ "${1##*.}" == "xml" ]]; then
  # The QC files to merge are taken from the input itself instead of from a fixed list, so
  # that the merging follows whatever set of QC tasks the simulation workflow produced for
  # this production. An intermediate stage writes them at the top level of its archive,
  # while an MC job keeps them in the QC subdirectory of its own.
  firstinput=$(sed -rn "s/.*turl=\"([^\"]*)\".*/\1/p" $1 | head -1)
  echo ${firstinput}
  mkdir -p filelist
  alien.py cp ${firstinput} file:filelist/
  echo "uncompress..."
  unzip -o filelist/$(basename ${firstinput}) -d filelist
  if [ $inputFromIntermediateStage -eq 1 ]; then
    declare -a qcfiles_list=($(cd filelist && ls *.root))
  else
    declare -a qcfiles_list=($(cd filelist/QC && ls *.root))
  fi

  for qcfile in "${qcfiles_list[@]}"; do
    qcfile_stripped="${qcfile%.*}"
    if [ $inputFromIntermediateStage -eq 1 ]; then
      sed -rn "s/.*turl=\"([^\"]*)\".*/\1#${qcfile}/p" $1 > list_${qcfile_stripped}.list
    else
      sed -rn "s/.*turl=\"([^\"]*)\".*/\1#QC\/${qcfile}/p" $1 > list_${qcfile_stripped}.list
    fi
    cat list_${qcfile_stripped}.list
    rm -f $qcfile
    o2-qc-file-merger --output-file ./$qcfile --input-files-list list_${qcfile_stripped}.list --enable-alien
  done
fi

#-------------------------------

# ----------- LOAD UTILITY FUNCTIONS --------------------------
. ${O2_ROOT}/share/scripts/jobutils.sh

# ----------- START ACTUAL JOB  -----------------------------

# create workflow
# the collision system is only known to this job through the LPM interaction type; without it
# the finalization would upload its objects with an empty beam type
beamTypeOption=""
[ ! -z "${ALIEN_JDL_LPMINTERACTIONTYPE}" ] && beamTypeOption="-beamType ${ALIEN_JDL_LPMINTERACTIONTYPE}"
${O2DPG_ROOT}/MC/bin/o2dpg_qc_finalization_workflow.py -o qc_finalization.json -run ${ALIEN_JDL_LPMRUNNUMBER} -productionTag ${ALIEN_JDL_LPMPRODUCTIONTAG} ${beamTypeOption}

# run workflow
mv -v *.root ./QC/
${O2DPG_ROOT}/MC/bin/o2_dpg_workflow_runner.py --keep-going -f qc_finalization.json
finalizationRC=$?
mv ./QC/*.root .
[ ${finalizationRC} -ne 0 ] && echo "Error: QC finalization finished with failing tasks, see the task logs under QC/"

# -----------------------------------------------------------

echo "Listing output"
ls -lh ./*

echo "du -h --max-depth=1 ."
du -h --max-depth=1 .

#
# full logs tar-ed for output, regardless the error code or validation
#
find ./ \( -name "*.log*" -o -name "*mergerlog*" -o -name "*serverlog*" -o -name "*workerlog*" \) | tar -czvf debug_log_archive.tgz -T -

# the merged files and the logs above are complete at this point, so a partial finalization
# can be reported without losing anything the job produced
exit ${finalizationRC}
