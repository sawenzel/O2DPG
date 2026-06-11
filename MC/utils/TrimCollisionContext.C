// Copyright 2019-2020 CERN and copyright holders of ALICE O2.
// See https://alice-o2.web.cern.ch/copyright for details of the copyright holders.
// All rights not expressly granted are reserved.
//
// This software is distributed under the terms of the GNU General Public
// License v3 (GPL Version 3), copied verbatim in the file "COPYING".
//
// In applying this license CERN does not waive the privileges and immunities
// granted to it by virtue of its status as an Intergovernmental Organization
// or submit itself to any jurisdiction.

//
// Usage:
//   root -q -b -l 'TrimCollisionContext.C("collisioncontext.root", <orbitFirstSampled>)'
//
// -----------------------------------------------------------------------------
// PURPOSE
// -----------------------------------------------------------------------------
// A per-timeframe collision context produced with --orbitsEarly is prefixed with
// the "early" collisions borrowed from the previous timeframe (their slow-particle
// hits leak into this timeframe's readout window, so they are needed for
// digitization). When the AOD producer reads such a context it emits BC,
// MCCollision and McParticle rows for those borrowed collisions as well, so the
// SAME generated collision appears in two consecutive timeframes' AODs. After
// merging the timeframes this shows up as duplicate / non-monotonic fGlobalBC and
// double-counted MC events.
//
// This macro removes the borrowed collisions from the context BEFORE AOD creation,
// so each timeframe owns a disjoint BC range and no collision is emitted twice.
// A collision is "borrowed" when its absolute global BC lies before the start of
// the owning timeframe, i.e. globalBC < orbitFirstSampled * LHCMaxBunches.
//
// The original file is backed up (default collisioncontext_backup.root) on first
// run; subsequent runs re-read the backup, so repeated invocations (e.g. on an AOD
// task re-run) are idempotent and always trim from the pristine context.
//
// NOTE on track labels: reconstructed tracks of this timeframe may still carry MC
// labels pointing into the removed collisions. Those collisions are no longer in
// the context, so the AOD producer cannot store their McParticles; the dangling
// labels must be invalidated (set to -1) inside the producer. This requires the
// companion safety-net in AODProducerWorkflowSpec::fillMCParticlesTable (reset
// keep-store entries of non-stored <source, event> to -1). Without that producer
// change the labels would resolve to a wrong McParticle row -- see the O2 branch
// swenzel/mc-coll-context-trim.
// -----------------------------------------------------------------------------

//#include "SimulationDataFormat/DigitizationContext.h"
//#include "CommonDataFormat/InteractionRecord.h"
//#include "CommonConstants/LHCConstants.h"

//#include <cstdint>
//#include <filesystem>
//#include <iostream>
//#include <vector>

// Trim the collisions of a per-timeframe collision context whose global BC lies
// before the start of the owning timeframe.
//   filename         : collision context to trim in place
//   orbitFirstSampled: first orbit of the owning timeframe (HBFUtils.orbitFirstSampled)
//   backupname       : where the pristine context is preserved (and re-read on re-run)
void TrimCollisionContext(const char* filename = "collisioncontext.root",
                          unsigned long orbitFirstSampled = 0,
                          const char* backupname = "collisioncontext_backup.root")
{
  namespace fs = std::filesystem;
  using o2::steer::DigitizationContext;
  using namespace o2;
    
  const int64_t cutBC = static_cast<int64_t>(orbitFirstSampled) * o2::constants::lhc::LHCMaxBunches;

  // Idempotent backup handling: on the first run preserve the original; on any
  // re-run start again from that pristine copy so we never trim twice.
  if (!fs::exists(backupname)) {
    if (!fs::exists(filename)) {
      std::cerr << "[TrimCollisionContext] input file not found: " << filename << std::endl;
      return;
    }
    fs::copy_file(filename, backupname, fs::copy_options::overwrite_existing);
    std::cout << "[TrimCollisionContext] backed up " << filename << " -> " << backupname << std::endl;
  } else {
    std::cout << "[TrimCollisionContext] backup present; re-reading pristine context from "
              << backupname << std::endl;
  }

  DigitizationContext* ctx = DigitizationContext::loadFromFile(backupname);
  if (!ctx) {
    std::cerr << "[TrimCollisionContext] could not load DigitizationContext from " << backupname << std::endl;
    return;
  }

  // Trim a (records, parts[, vertices]) triplet in lockstep, keeping only
  // collisions with globalBC >= cutBC. Event IDs are NOT renumbered: the
  // kinematics files still hold all events and the AOD producer reads tracks by
  // <source, event>, so the surviving IDs must stay as they are.
  auto trim = [cutBC](std::vector<o2::InteractionTimeRecord>& records,
                      std::vector<std::vector<o2::steer::EventPart>>& parts,
                      const std::vector<math_utils::Point3D<float>>& vertices,
                      std::vector<math_utils::Point3D<float>>& outVertices) -> size_t {
    const bool haveVertices = vertices.size() == records.size();
    std::vector<o2::InteractionTimeRecord> keptRecords;
    std::vector<std::vector<o2::steer::EventPart>> keptParts;
    outVertices.clear();
    keptRecords.reserve(records.size());
    keptParts.reserve(parts.size());
    size_t dropped = 0;
    for (size_t i = 0; i < records.size(); ++i) {
      if (records[i].toLong() < cutBC) {
        ++dropped;
        continue;
      }
      keptRecords.push_back(records[i]);
      keptParts.push_back(parts[i]);
      if (haveVertices) {
        outVertices.push_back(vertices[i]);
      }
    }
    records.swap(keptRecords);
    parts.swap(keptParts);
    return dropped;
  };

  const size_t nBefore = ctx->getEventRecords(false).size();

  // main (non-QED) collisions
  std::vector<math_utils::Point3D<float>> keptVertices;
  size_t dropped = trim(ctx->getEventRecords(false), ctx->getEventParts(false),
                        ctx->getInteractionVertices(), keptVertices);
  ctx->setNCollisions(ctx->getEventRecords(false).size());
  if (!keptVertices.empty()) {
    ctx->setInteractionVertices(keptVertices);
  }

  // QED-interleaved collisions, if present
  if (ctx->isQEDProvided()) {
    std::vector<math_utils::Point3D<float>> dummy; // QED records carry no vertices
    size_t droppedQED = trim(ctx->getEventRecords(true), ctx->getEventParts(true), dummy, dummy);
    std::cout << "[TrimCollisionContext] dropped " << droppedQED << " borrowed QED collisions" << std::endl;
  }

  const size_t nAfter = ctx->getEventRecords(false).size();
  std::cout << "[TrimCollisionContext] cut BC = " << cutBC
            << " (orbitFirstSampled = " << orbitFirstSampled << ")"
            << "; collisions " << nBefore << " -> " << nAfter
            << " (dropped " << dropped << ")" << std::endl;

  ctx->saveToFile(filename);
  delete ctx;
}
