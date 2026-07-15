.PHONY: check unit contract safety

check: unit contract

unit:
	python3 -m unittest discover -s tests/unit -p 'test_*.py'

contract:
	python3 -m unittest discover -s tests/contract -p 'test_*.py'

safety:
	python3 tests/contract/test_repository_safety.py
