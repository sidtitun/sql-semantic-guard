"""Driver bindings preserve SQL order and tenant isolation."""
import sqlite3

import pytest

from sqlguard import Catalog, Policy, PolicyOverlay, PolicySet, RLSRule, SQLGuard
from sqlguard.errors import PolicyError


@pytest.mark.parametrize('style', ['named', 'pyformat', 'qmark', 'numeric'])
def test_styles_and_repeated_names(style):
    guard = SQLGuard(Catalog.from_dict({}), Policy(paramstyle=style, default_limit=None))
    result = guard.validate('WITH x AS (SELECT :a AS a) SELECT :b, :a FROM x', {'a': 5, 'b': 9})
    assert result.valid, result.feedback()
    values = list(result.parameters.values()) if isinstance(result.parameters, dict) else result.parameters
    assert values == [5, 9, 5]
    assert 'parameters' not in result.to_dict()
    assert result.to_dict(include_parameters=True)['parameters'] == result.parameters
    for i in range(1, 4):
        marker = {'named': f':p{i}', 'pyformat': f'%(p{i})s', 'numeric': f':{i}', 'qmark': '?'}[style]
        assert marker in result.sql


@pytest.mark.parametrize('style', ['qmark', 'named'])
def test_real_sqlite_execution_with_rls_and_list(style):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE orders (id INTEGER, tenant INTEGER)')
    db.executemany('INSERT INTO orders VALUES (?, ?)', [(1, 7), (2, 8), (3, 7)])
    guard = SQLGuard(Catalog.from_dict({'orders': {'id': 'int', 'tenant': 'int'}}), Policy(
        paramstyle=style, rls=[RLSRule(table='orders', predicate='tenant IN :tenants')],
        default_limit=None, on_missing_stats='ignore',
    ), dialect='sqlite')
    result = guard.validate('SELECT id FROM orders WHERE id > :min ORDER BY id', {'tenants': [7], 'min': 0})
    assert result.valid, result.feedback()
    assert db.execute(result.sql, result.parameters).fetchall() == [(1,), (3,)]
    db.close()


def test_scalar_rls_role_and_collision_safe_bind_names():
    guard = SQLGuard(Catalog.from_dict({'orders': {'tenant': 'int'}}), PolicySet(
        Policy(paramstyle='qmark', default_limit=None, on_missing_stats='ignore'),
        {'reader': PolicyOverlay(add_rls=[RLSRule(table='orders', column='tenant', param='tenant')])},
    ))
    result = guard.validate('SELECT tenant FROM orders WHERE tenant > :p1', {'p1': 0, 'tenant': 7}, role='reader')
    assert result.valid, result.feedback()
    assert result.parameters == [0, 7]
    assert '7' not in result.sql


@pytest.mark.parametrize('sql,params', [('SELECT :missing', {}), ('SELECT ?', {}), ('SELECT $1', {'1': 2})])
def test_unbound_inputs_fail_closed_in_shadow(sql, params):
    result = SQLGuard(Catalog.from_dict({}), Policy(paramstyle='qmark', enforcement='log_only')).validate(sql, params)
    assert not result.valid and result.sql is None and result.parameters is None


def test_strings_and_percent_signs_are_not_placeholders():
    guard = SQLGuard(Catalog.from_dict({}), Policy(paramstyle='pyformat', default_limit=None))
    result = guard.validate("SELECT ':fake 50%' AS text, :real", {'real': "O'Reilly"})
    assert result.valid, result.feedback()
    assert "':fake 50%%'" in result.sql
    assert result.parameters == {'p1': "O'Reilly"}


@pytest.mark.parametrize("enforcement", ["block", "log_only"])
def test_missing_rls_parameter_fails_closed(enforcement):
    guard = SQLGuard(Catalog.from_dict({'orders': {'tenant': 'int'}}), Policy(
        paramstyle='qmark', rls_parameterize=True, enforcement=enforcement,
        rls=[RLSRule(table='orders', column='tenant', param='tenant')],
    ))
    result = guard.validate('SELECT tenant FROM orders')
    assert not result.valid and result.parameters is None


def test_defaults_and_no_bindings_are_unchanged():
    catalog = Catalog.from_dict({})
    assert SQLGuard(catalog).validate('SELECT 1').parameters is None
    assert SQLGuard(catalog, Policy(paramstyle='qmark')).validate('SELECT 1').parameters is None
    with pytest.raises(PolicyError, match='paramstyle'):
        Policy(paramstyle='invalid')


def test_multi_value_rls_and_hostile_strings_stay_bound():
    catalog = Catalog.from_dict({'orders': {'tenant': 'text'}})
    for rule in [RLSRule(table='orders', column='tenant', param='tenants'),
                 RLSRule(table='orders', predicate='tenant IN (:tenants)')]:
        guard = SQLGuard(catalog, Policy(paramstyle='qmark', rls=[rule], on_missing_stats='ignore'))
        values = ["x' OR 1=1 --", 'second']
        result = guard.validate('SELECT tenant FROM orders', {'tenants': values})
        assert result.valid, result.feedback()
        assert result.parameters == values
        assert "OR 1=1" not in result.sql


def test_estimator_not_called_for_missing_binding():
    class Estimator:
        def estimate(self, *args):
            raise AssertionError('must not estimate an unbound query')

    guard = SQLGuard(Catalog.from_dict({}), Policy(paramstyle='named'), estimators=[Estimator()])
    result = guard.validate('SELECT :missing')
    assert not result.valid
    assert [v.code.value for v in result.errors] == ['parameter_error']
