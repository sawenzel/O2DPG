We made awesome progress and are converging. But we have to be careful and fix some things
systematically. First of all, these are the problems I found and that I'd like you to work on:

(a) The overall runtime measured by o2dpg_sim_metric.py stat, using the iteration count in the metric file,
    currently underestimates the walltime. This time should be the same as the time reported in the last line 
    of pipeline_action. I guess iterations take slightly longer than 1s and we need to find out how. Timestamps
    in the metrics file might help.

(b) Concerning Amdahl. Yes, we've establish that some task can be worker scaled. So far we are using O2DPG_DYNAMIC_WORKERS.
    Maybe we should mark them with a simple "amdahl_scalable" flag in the json. However, what you got wrong is the base number of workers
    for your Amdahl calculation. For sgnsim it is not 7 (that is just a resource estimate for booking)... it is in fact 8 or some other number
    only mentioned in the 'cmd' of workflow.json. So this needs to be corrected.
    We can then directly derive Amdahl numbers (serial overhead) in the learned.json

(c) The simulator should be improved further:
    - a verbose mode outputing estimated walltime from amdahl
    - fix inconsistency in default makespan estimates between optimize workers mode and
      and the default mode (they are not the same but should be).