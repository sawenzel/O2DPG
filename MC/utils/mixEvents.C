// a tool to generate a single kinematics file
// from interleaving multiple files with ratio
// consider taking a collision context and producing event mix
void mixEvents(const char* fileName1, const char* fileName2, const char* outFileName, int ratio) {
  if (!fileName1) {
    return;
  }
  if (!fileName2) {
    return;
  }
  if (!outFileName) {
    return;
  }

  const char* trackBranchName = "MCTrack";
  std::vector<o2::MCTrack>* readEvent = nullptr;

  int eventCount = 0;
  int eventCount1 = 0;
  int eventCount2 = 0;

  TFile f1In(fileName1, "OPEN");
  TFile f2In(fileName2, "OPEN");

  auto t1In = (TTree*) f1In.Get("o2sim");
  auto t2In = (TTree*) f2In.Get("o2sim");
  if (!t1In || !t2In) {
    return;
  }

  auto b1In = t1In->GetBranch(trackBranchName);
  auto b2In = t2In->GetBranch(trackBranchName);
  if (!b1In || !b2In) {
    return;
  }

  auto outFile = new TFile(outFileName, "RECREATE");
  auto outTree = new TTree("o2sim", "o2sim");
  if (!outTree) {
    std::cerr << "No outtree";
    return;
  }
  TBranch* outBranch = outTree->Branch(trackBranchName, &readEvent);
  if (!outBranch) {
    std::cerr << "No outbranch";
    return;
  }

  outTree->SetDirectory(outFile);
  std::cout << "Found " << b1In->GetEntries() << " events in " << fileName1 << "\n";
  std::cout << "Found " << b2In->GetEntries() << " events in " << fileName2 << "\n";

  while (true) {
    if (eventCount % ratio == 0) {
      if (eventCount1 < b1In->GetEntries()) {
        b1In->SetAddress(&readEvent);
        b1In->GetEntry(eventCount1++);
      }
      else {
	std::cout << "No more events of type 1 .. breaking\n ";
        break;
      }
    }
    else {
      if (eventCount2 < b2In->GetEntries()) {
        b2In->SetAddress(&readEvent);
        b2In->GetEntry(eventCount2++);
      }
      else {
	std::cout << "No more events of type 2 .. breaking\n ";
        break;
      }
    }
    outBranch->Fill();
    if (readEvent) {
      delete readEvent;
      readEvent = nullptr;
    }
    eventCount++;
  }

  std::cout << "Generated " << eventCount << " events\n";
  f1In.Close();
  f2In.Close();
  outTree->SetDirectory(outFile);
  outTree->Write();
  outFile->Close();
}
