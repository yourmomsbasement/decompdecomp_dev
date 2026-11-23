"""Regional evidence retains scope, negative results and model counterexamples without approving matches."""

import json
from types import SimpleNamespace

import pytest

from decompnet.app import region_work
from decompnet.codeblocks import Instruction
from decompnet.network import NetworkError
from tests import test_compile_operations as operation_tests


@pytest.fixture
def workflow(tmp_path,monkeypatch):
    yield from operation_tests.rig.__wrapped__(tmp_path,monkeypatch)


@pytest.fixture
def region(workflow,monkeypatch):
    rig=workflow
    fn=rig.net._function('work','demo')
    code=[Instruction(0,'mov eax, 1'),Instruction(5,'ret'),Instruction(6,'mov eax, 2'),Instruction(11,'ret')]
    monkeypatch.setattr(region_work.compile_tools,'snapshot',lambda *a:(fn,'int work(){return 1;}','rev'))
    monkeypatch.setattr(region_work,'target',lambda *a:(code,None,'target-digest'))
    return rig,code


def test_region_evidence_is_shared_and_records_negative_hypotheses(region):
    rig,_=region
    result=region_work.record(rig.net,rig.owner,'demo','work','rev',0,5,'Change one constant','Target returns a different constant','falsified','Compiled identically')
    assert result.startswith('R1: falsified')
    text=region_work.history(rig.net,'demo','work')
    assert 'falsified' in text and 'Compiled identically' in text and 'revision rev' in text


def test_region_rejects_stale_revision_and_noninstruction_offsets(region):
    rig,_=region
    with pytest.raises(NetworkError,match='Stale revision'):
        region_work.record(rig.net,rig.owner,'demo','work','old',0,5,'Change one constant','Target returns a different constant')
    with pytest.raises(NetworkError,match='instruction offsets'):
        region_work.record(rig.net,rig.owner,'demo','work','rev',1,5,'Change one constant','Target returns a different constant')


def test_comparison_reports_improvement_and_regression_outside_region(region):
    _,code=region
    before=[('|','mov eax, 1','mov eax, 0'),(' ','ret','ret'),(' ','mov eax, 2','mov eax, 2'),(' ','ret','ret')]
    after=[(' ','mov eax, 1','mov eax, 1'),(' ','ret','ret'),('|','mov eax, 2','mov eax, 3'),(' ','ret','ret')]
    result=region_work.compare(code,'x86_64',None,before,after,0,5)
    assert '1->0 differing rows (selected region)' in result
    assert '0->1 differing rows (outside region)' in result
    assert 'Advisory' in result


def test_comparison_abstains_when_a_target_row_changed(region):
    _,code=region
    text=region_work.compare(code,'x86_64',None,[('|','stale','anything')],[('|','stale','anything')],0,5)
    assert 'Unavailable' in text


def test_refcount_boundary_model_finds_wraparound_counterexample(region):
    rig,_=region
    region_work.record(rig.net,rig.owner,'demo','work','rev',0,5,'Check counter overflow','Target checks zero after increment')
    increment=['u16',['add','n',1]]
    target=['select',['eq',increment,0],0,['sub',increment,1]]
    candidate=['u16',['sub',increment,1]]
    result=json.loads(region_work.check(rig.net,rig.owner,'demo','work',1,target,candidate,[{'n':1},{'n':65534},{'n':65535}]).split('\n')[0])
    assert result['mismatch_count']==1
    assert result['counterexamples'][0]['inputs']=={'n':65535}
    assert 'supplied integer models only' in result['scope']
    assert 'counterexamples' in region_work.history(rig.net,'demo','work')


def test_integer_model_rejects_code_execution_and_overlarge_values(region):
    with pytest.raises(NetworkError):region_work.evaluate(['exec','anything'],{})
    with pytest.raises(NetworkError):region_work.evaluate(2**200,{})
    assert region_work.evaluate(['i16',65535],{})==-1
    assert region_work.evaluate(['u64',-1],{})==2**64-1


def test_region_binding_enforces_owner_revision_and_single_operation(region):
    rig,_=region
    region_work.record(rig.net,rig.owner,'demo','work','rev',0,5,'Change one constant','Target returns a different constant')
    fn=rig.net._function('work','demo')
    with rig.net.store.tx() as db:
        region_work.bind(db,fn,rig.owner,1,'rev')
        with pytest.raises(NetworkError):region_work.bind(db,fn,SimpleNamespace(id=2),1,'rev')
        with pytest.raises(NetworkError):region_work.bind(db,fn,rig.owner,1,'old')
        db.execute("UPDATE region_experiments SET operation_id='op' WHERE id=1")
        with pytest.raises(NetworkError):region_work.bind(db,fn,rig.owner,1,'rev')
