#!/bin/bash

set -o pipefail

function per() { printf "\033[31m$1\033[m\n" >&2; }
function pok() { printf "\033[32m$1\033[m\n" >&2; }
function banner() { echo ; echo ==================== $1 ==================== ; }

if [[ ! $ALIEN_PROC_ID && ! $1 ]]; then
   per "Please give a job name"
   exit 1
fi

# General job configuration
MY_USER=aliperf #$( (alien_whoami || true) 2> /dev/null | xargs echo )
if [[ ! $MY_USER ]]; then
  per "Problems retrieving current AliEn user. Did you run alien-token-init?"
  exit 1
fi
MY_HOMEDIR="/alice/cern.ch/user/${MY_USER:0:1}/${MY_USER}"
MY_BINDIR="$MY_HOMEDIR/bin"
MY_JOBPREFIX="$MY_HOMEDIR/selfjobs"
MY_JOBNAME=${1:-job}
MY_JOBNAMEDATE="${MY_JOBNAME}-$(date -u +%Y%m%d-%H%M%S)"
MY_JOBWORKDIR="$MY_JOBPREFIX/${MY_JOBNAMEDATE}"  # ISO-8601 UTC
MY_JOBWORKDIR_TMP="${MY_JOBWORKDIR}_TMP"  # for temporary uploads

# TODO validate MY_JOBNAME

pok "Your job's working directory will be $MY_JOBWORKDIR"
pok "Set the job name by running $0 <jobname>"

# 
# Submitter code
#
if [[ ! $ALIEN_PROC_ID ]]; then
  # We are not on a worker node: assuming client --> test if alien is there?
  which alien.py
  # check exit code
  if [[ ! "$?" == "0"  ]]; then
    XJALIEN_LATEST=`find /cvmfs/alice.cern.ch/el7-x86_64/Modules/modulefiles/xjalienfs -type f -printf "%f\n" | tail -n1`
    banner "Loading xjalien package $XJALIEN_LATEST"
    eval "$(/cvmfs/alice.cern.ch/bin/alienv printenv xjalienfs::"$XJALIEN_LATEST")"
  fi

  # Create workdir, and work there
  cd "$(dirname "$0")"
  THIS_SCRIPT="$PWD/$(basename "$0")"
  mkdir -p work/$(basename "$MY_JOBWORKDIR")
  cd work/$(basename "$MY_JOBWORKDIR")

  # Generate JDL
  cat > "${MY_JOBNAMEDATE}.jdl" <<EOF
Executable = "${MY_BINDIR}/${MY_JOBNAMEDATE}.sh";
OutputDir = "${MY_JOBWORKDIR}";
Output = {
  "*.log*,log.txt@disk=2"
};
Requirements = member(other.GridPartitions,"multicore_8");
MemorySize = "60GB";
TTL=80000;
EOF
#
  pok "Local working directory is $PWD"

  pok "Preparing job \"$MY_JOBNAMEDATE\""
  (
    alien.py rmdir "$MY_JOBWORKDIR" || true                                   # remove existing job dir
    alien.py mkdir "${MY_JOBWORKDIR_TMP}" || true
    alien.py mkdir "$MY_BINDIR" || true                                       # create bindir
    alien.py mkdir "$MY_JOBPREFIX" || true                                    # create job output prefix
    alien.py mkdir jdl || true
    alien.py rm "$MY_BINDIR/${MY_JOBNAMEDATE}.sh" || true                     # remove current job script
    alien.py cp "${PWD}/${MY_JOBNAMEDATE}.jdl" alien://${MY_HOMEDIR}/jdl/${MY_JOBNAMEDATE}.jdl@ALICE::CERN::EOS || true  # copy the jdl
    alien.py cp "$THIS_SCRIPT" alien://${MY_BINDIR}/${MY_JOBNAMEDATE}.sh@ALICE::CERN::EOS || true  # copy current job script to AliEn
  ) &> alienlog.txt

  pok "Submitting job \"${MY_JOBNAMEDATE}\" from $PWD"
  (
    alien.py submit jdl/${MY_JOBNAMEDATE}.jdl || true
  ) &>> alienlog.txt

  MY_JOBID=$( (grep 'Your new job ID is' alienlog.txt | grep -oE '[0-9]+' || true) | sort -n | tail -n1)
  if [[ $MY_JOBID ]]; then
    pok "OK, display progress on https://alimonitor.cern.ch/agent/jobs/details.jsp?pid=$MY_JOBID"
  else
    per "Job submission failed: error log follows"
    cat alienlog.txt
  fi

  exit 0
fi

####################################################################################################
# The following part is executed on the worker node
####################################################################################################

# All is redirected to log.txt but kept on stdout as well
if [[ $ALIEN_PROC_ID ]]; then
  exec &> >(tee -a log.txt)
fi

# ----------- LOAD UTILITY FUNCTIONS --------------------------
. ${O2_ROOT}/share/scripts/jobutils.sh


# ----------- START JOB PREAMBLE  ----------------------------- 

sbanner "Environment"
env

banner "OS detection"
lsb_release -a || true
cat /etc/os-release || true
cat /etc/redhat-release || true

O2_PACKAGE_LATEST=`find /cvmfs/alice.cern.ch/el7-x86_64/Modules/modulefiles/O2 -type f -printf "%f\n" | tail -n1`
XJALIEN_LATEST=`find /cvmfs/alice.cern.ch/el7-x86_64/Modules/modulefiles/xjalienfs -type f -printf "%f\n" | tail -n1`
O2DPG_LATEST=`find /cvmfs/alice.cern.ch/el7-x86_64/Modules/modulefiles/O2DPG -type f -printf "%f\n" | tail -n1`
banner "Loading O2 package $O2_PACKAGE_LATEST"
eval "$(/cvmfs/alice.cern.ch/bin/alienv printenv O2::"$O2_PACKAGE_LATEST",xjalienfs::"$XJALIEN_LATEST",O2DPG::"$O2DPG_LATEST")"

banner "Running workflow"

host=`hostname`
ldd `which o2-sim` > ldd.log
o2exec=`which o2-sim`

cat /proc/cpuinfo > cpuinfo.log 
cat /proc/meminfo > meminfo.log

# ----------- START ACTUAL JOB  ----------------------------- 

NSIGEVENTS=${NSIGEVENTS:-20}
NTIMEFRAMES=${NTIMEFRAMES:-5}
NWORKERS=${NWORKERS:-8}
NBKGEVENTS=${NBKGEVENTS:-20}
MODULES="--skipModules ZDC"


# background task -------
taskwrapper bkgsim.log o2-sim -j ${NWORKERS} -n ${NBKGEVENTS} -g pythia8hi ${MODULES} -o bkg \
            --configFile ${O2DPG_ROOT}/MC/config/common/ini/basic.ini

# loop over timeframes
for tf in `seq 1 ${NTIMEFRAMES}`; do

  RNDSEED=0
  PTHATMIN=0.  # [default = 0]
  PTHATMAX=-1. # [default = -1]

  # produce the signal configuration
  ${O2DPG_ROOT}/MC/config/common/pythia8/utils/mkpy8cfg.py \
    	     --output=pythia8.cfg \
	     --seed=${RNDSEED} \
	     --idA=2212 \
	     --idB=2212 \
	     --eCM=13000. \
	     --process=ccbar \
	     --ptHatMin=${PTHATMIN} \
	     --ptHatMax=${PTHATMAX}
 
  # simulate the signals for this timeframe
  taskwrapper sgnsim_${tf}.log o2-sim ${MODULES} -n ${NSIGEVENTS} -e TGeant3 -j ${NWORKERS} -g extgen \
       --configFile ${O2DPG_ROOT}/MC/config/PWGHF/ini/GeneratorHF.ini                                 \
       --configKeyValues "GeneratorPythia8.config=pythia8.cfg"                                        \
       --embedIntoFile bkg_Kine.root                                                                  \
       -o sgn${tf}

  CONTEXTFILE=collisioncontext_${tf}.root

  cp sgn${tf}_grp.root o2sim_grp.root

  # now run digitization phase
  echo "Running digitization for $intRate kHz interaction rate"
  
  gloOpt="-b --run --shm-segment-size 10000000000" # TODO: decide shared mem based on event number

  taskwrapper tpcdigi_${tf}.log o2-sim-digitizer-workflow $gloOpt -n ${NSIGEVENTS} --sims bkg,sgn${tf} --onlyDet TPC --interactionRate 50000 --tpc-lanes ${NWORKERS} --outcontext ${CONTEXTFILE}
  # --> a) random seeding
  # --> b) propagation of collisioncontext and application in other digitization steps

  echo "Return status of TPC digitization: $?"
  taskwrapper trddigi_${tf}.log o2-sim-digitizer-workflow $gloOpt -n ${NSIGEVENTS} --sims bkg,sgn${tf} --onlyDet TRD --interactionRate 50000 --configKeyValues "TRDSimParams.digithreads=10" --incontext ${CONTEXTFILE}
  echo "Return status of TRD digitization: $?"

  taskwrapper restdigi_${tf}.log o2-sim-digitizer-workflow $gloOpt -n ${NSIGEVENTS} --sims bkg,sgn${tf} --skipDet TRD,TPC --interactionRate 50000 --incontext ${CONTEXTFILE}
  echo "Return status of OTHER digitization: $?"

  taskwrapper tpcreco_${tf}.log o2-tpc-reco-workflow $gloOpt --tpc-digit-reader \"--infile tpcdigits.root\" --input-type digits --output-type clusters,tracks  --tpc-track-writer \"--treename events --track-branch-name Tracks --trackmc-branch-name TracksMCTruth\" --configKeyValues \"GPU_global.continuousMaxTimeBin=10000\"
  echo "Return status of tpcreco: $?"

  # we need to move these products somewhere
  mv tpctracks.root tpctracks_${tf}.root
  mv tpcdigits.root tpcdigits_${tf}.root
done

# We need to exit for the ALIEN JOB HANDLER!
exit 0
