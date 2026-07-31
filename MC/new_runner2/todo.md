# fix `--dynamic-resources` code path
# ML metric prediction project
# check other things such as script production, dry-run, IO file removal
# suggest dimensioning of MC production jobs based on learned metrics
  - "The job can be run with N timeframes per job ... based on scheduling policy simulation and learned resources".
  - "In any case we should probably target N=8 minimum or multiples thereof"
# automatic analysis of metrics and comparisonq with input learned or given metrics
# using the file io tool --> discovery of better graph structure?
# given that we have simulator --> parameter optimization (how does changing nworkers for sgnsim etc impact cpu-efficiency)? (DONE)
# policy for how/which job to background?
# fix back to use of CPUQuota + tasksetting instead of writing to cpu.max (does not work on SLC9)
# include disc usage in simulator (learn disc usage per task with fanotify)
# address systemd-run failures like `2026-05-01 10:43:11,992 INFO [systemd] sgnsim_8: Failed to start transient scope unit: Unit task-sgnsim_8-250.scope was already loaded or has a fragment file.`
# nworker parameter optimization will favour completely parallel timeframes at 1-core. But this will increase memory. So we'll have to find a sweet spot compatible with memory ... OR at least take into account memory MODEL

# consider TOTAL memory in scheduling
# have a memory model for each task as a function of input paremeters
# memory based management: abort backfilled task if this helps freeing up memory. Thereafter abort normal tasks until situation improved. We could use swap?