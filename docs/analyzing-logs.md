When analyzing ./Scripts/m1n1/logs/<RUN>/{run.log,nic-runtime.txt} files there are smart ways of doing things and stupid ways of doing things
git --no-pager diff --no-index ./Scripts/m1n1/logs/<RUN_A>/run.log ./Scripts/m1n1/logs/<RUN_B>/run.log
then look through git history to find when and where the RUN_{A,B} commits were to and see what changed!
then analyze the python script change against the logs
use git show <COMMIT> --stat to find where RUN_{A,B} changes were made
and then git show <COMMIT> <FILE> to see what changed
analyze the logs properly!
use rg as it is often much more useful than grep
use fd as it is often much more useful than find
