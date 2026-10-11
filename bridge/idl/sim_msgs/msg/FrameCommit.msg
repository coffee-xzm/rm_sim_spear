# Source identity for one externally visible simulation commit.
# sim_time is the MuJoCo time; it is also the timestamp used by standard
# sensor messages emitted for this commit.
string run_id
uint64 commit_id
uint64 sim_tick
builtin_interfaces/Time sim_time
uint64 physics_step
uint64 state_revision
