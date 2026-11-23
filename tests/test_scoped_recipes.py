"""A saved donor observation is scoped evidence, never an automatic code rewrite or current match verdict."""

from types import SimpleNamespace

import pytest

from decompnet.app import compile_tools, scoped_recipes
from decompnet.network import NetworkError
from tests import test_compile_operations as operation_tests


@pytest.fixture
def workflow(tmp_path,monkeypatch):
    yield from operation_tests.rig.__wrapped__(tmp_path,monkeypatch)


@pytest.fixture
def recipes(workflow,tmp_path,monkeypatch):
    rig=workflow
    (tmp_path/'target.o').write_bytes(b'synthetic-target')
    fp=SimpleNamespace(root=tmp_path,unit=lambda name:SimpleNamespace(target=tmp_path/'target.o'))
    rig.net.forks.get=lambda slug:({'id':1,'slug':'demo'},fp,{'platform':'test','compiler':'configured'})
    with rig.net.store.tx() as db:
        db.execute("INSERT INTO functions(id,project_id,name,unit,status) VALUES(2,1,'donor','unit','matched')")
    rig.net._function=lambda name,slug:rig.net.store.one('SELECT * FROM functions WHERE name=?',name)
    monkeypatch.setattr(compile_tools,'snapshot',lambda *a:(None,'int work(){return 1;}','rev'))
    from contextlib import nullcontext
    monkeypatch.setattr(compile_tools,'_locked',lambda *a,**k:nullcontext())  # (the stand-in engine has no locks)
    args=dict(name='one constant',prerequisites='Only where the target returns two',evidence='Recorded donor returns two strictly',donors=['donor'],edits=[{'old':'return 1','new':'return 2'}])
    return rig,tmp_path,args


def test_recipe_retrieval_preserves_prerequisites_without_applying(recipes):
    rig,_,args=recipes
    assert 'S1' in scoped_recipes.save(rig.net,rig.owner,'demo',**args)
    result=scoped_recipes.candidates(rig.net,'demo','work')
    assert 'Only where the target returns two' in result and 'not fresh verdicts' in result
    assert 'try_recipes' in result and 'return 2' in result


def test_recipe_is_not_suggested_after_build_scope_changes(recipes):
    rig,path,args=recipes
    scoped_recipes.save(rig.net,rig.owner,'demo',**args)
    (path/'build.ninja').write_text('changed compiler flags')
    assert 'No proposal fits' in scoped_recipes.candidates(rig.net,'demo','work')


def test_changed_target_or_reopened_donor_invalidates_proposal(recipes):
    rig,path,args=recipes
    scoped_recipes.save(rig.net,rig.owner,'demo',**args)
    (path/'target.o').write_bytes(b'different-target')
    assert 'No proposal fits' in scoped_recipes.candidates(rig.net,'demo','work')
    with rig.net.store.tx() as db:db.execute("UPDATE functions SET status='open' WHERE id=2")
    with pytest.raises(NetworkError,match='matched status'):
        scoped_recipes.save(rig.net,rig.owner,'demo',**args)
