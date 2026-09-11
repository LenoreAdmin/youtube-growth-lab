import pytest
from app.experiments import thompson_proposal

def test_bandit_only_proposes_and_remains_reproducible():
    result=thompson_proposal({"a":(10,2),"b":(2,10)},seed=1)
    assert result["requires_human_approval"] is True
    assert result==thompson_proposal({"a":(10,2),"b":(2,10)},seed=1)

def test_invalid_outcome_counts_rejected():
    with pytest.raises(ValueError):
        thompson_proposal({"a":(-1,1)})
