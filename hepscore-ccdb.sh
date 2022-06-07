package: HEPscore-CCDB
version: v0.1.2-test
build_requires:
  - O2sim
  - alibuild-recipe-tools
---
#!/bin/bash

# A build recipe producing a CCDB snapshot for the HEPscore benchmark.
# - publish CCDB snapshot on CVFMS via alibuild/jenkins
# - allow execution of benchmark without ALIEN token/network

export ROOT_INCLUDE_PATH=${ROOT_INCLUDE_PATH}:${PYTHIA_ROOT}/include
export ROOT_INCLUDE_PATH=${ROOT_INCLUDE_PATH}:${HEPMC3_ROOT}/include 
export ROOT_INCLUDE_PATH=${ROOT_INCLUDE_PATH}:${FAIRMQ_ROOT}/include
export ROOT_INCLUDE_PATH=${ROOT_INCLUDE_PATH}:${TBB_ROOT}/include
# this is to access tokens and alien
export PYTHONPATH=${PYTHONPATH}:${XJALIENFS_ROOT}/lib/python/site-packages
export X509_USER_CERT=/etc/grid-security/hostcert.pem 
export X509_USER_KEY=/etc/grid-security/hostkey.pem 
export X509_CERT_DIR=/etc/grid-security/certificates
export PATH=${PATH}:${XJALIENFS_ROOT}/bin

env | tr ":" "\n" > runenv.log

# Set Geant4 data sets environment
[ "$G4INSTALL" != "" ] && \
`$G4INSTALL/bin/geant4-config --datasets | sed 's/[^ ]* //' | sed 's/G4/export G4/' | sed 's/DATA /DATA=/'`

# check if we can access alien
# alien.py ls /alice/cern.ch/user/a/aliperf/
alien-token-info || true

# launch the reference simulation (to fetch all needed CCDB objects and to cache them)
export ALICE_O2SIM_DUMPLOG=ON
NSIGEVENTS=1 NBKGEVENTS=1 NTIMEFRAMES=1 ${O2DPG_ROOT}/MC/run/HEPscore/hep-score.sh
cp bkg_serverlog bkg_serverlog.log
cp bkg_workerlog0 bkg_workerlog0.log
cp bkg_mergerlog bkg_mergerlog.log

# install artefacts
rsync -a --delete .ccdb $INSTALLROOT
mkdir -p $INSTALLROOT/data && rsync -a --delete matbud.root $INSTALLROOT/data

# Modulefile
mkdir -p $INSTALLROOT/etc/modulefiles
alibuild-generate-module > $INSTALLROOT/etc/modulefiles/$PKGNAME

cat << EOF >> $INSTALLROOT/etc/modulefiles/$PKGNAME
set HEPSCORE_CCDB_ROOT \$::env(BASEDIR)/$PKGNAME/\$version
setenv HEPSCORE_CCDB_ROOT \$HEPSCORE_CCDB_ROOT
setenv HEPSCORE_CCDB_VERSION $PKGVERSION
EOF
