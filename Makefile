# The /slots commands from a checkout. `make` alone lists them.
PYTHON ?= python3
GUARD := $(PYTHON) guard.py

.DEFAULT_GOAL := help
.PHONY: help status agents max auto quantum reset evict sprint sprint-off test

help:
	@echo 'Slot Guard from a checkout, the same as /slots in Claude Code:'
	@echo
	@echo '  make status              Show the limit, who is working and who waits    (/slots)'
	@echo '  make agents N=4          Set the normal limit to N agents                 (/slots agents N)'
	@echo '  make max N=6             Extra room above "agents" with auto on           (/slots max N)'
	@echo '  make auto ON=on|off      Pace the limit by the usage windows, or fix it   (/slots auto on|off)'
	@echo '  make quantum N=60        Seconds a slot is kept while others wait         (/slots quantum N)'
	@echo '  make reset               Forget usage readings, restart from "agents"     (/slots reset)'
	@echo '  make evict SEQS="12 13"  Release tickets by number, as listed by status   (/slots evict N...)'
	@echo '  make sprint [M=5]        Every session starts at once, for M minutes      (/slots sprint all M)'
	@echo '  make sprint-off          End every sprint now                             (/slots sprint off)'
	@echo '  make test                Run the Python tests'

status:
	@$(GUARD) status

agents:
	@test -n "$(N)" || { echo 'Usage: make agents N=4'; exit 2; }
	@$(GUARD) configure --agents $(N)

max:
	@test -n "$(N)" || { echo 'Usage: make max N=6'; exit 2; }
	@$(GUARD) configure --max $(N)

auto:
	@test "$(ON)" = on -o "$(ON)" = off || { echo 'Usage: make auto ON=on|off'; exit 2; }
	@$(GUARD) configure --auto $(ON)

quantum:
	@test -n "$(N)" || { echo 'Usage: make quantum N=60'; exit 2; }
	@$(GUARD) configure --quantum $(N)

reset:
	@$(GUARD) reset

evict:
	@test -n "$(SEQS)" || { echo 'Usage: make evict SEQS="12 13"'; exit 2; }
	@$(GUARD) evict $(SEQS)

sprint:
	@$(GUARD) sprint $(or $(M),5)

sprint-off:
	@$(GUARD) sprint 0

test:
	@$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
