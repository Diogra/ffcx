# Copyright (C) 2013-2017 Martin Sandve Alnæs
#
# This file is part of FFCX.(https://www.fenicsproject.org)
#
# SPDX-License-Identifier:    LGPL-3.0-or-later
"""Main algorithm for building the uflacs intermediate representation."""

import collections
import itertools
import logging

import numpy

import ufl
from ffcx.ir.uflacs.analysis.factorization import \
    compute_argument_factorization
from ffcx.ir.uflacs.analysis.graph import build_scalar_graph
from ffcx.ir.uflacs.analysis.modified_terminals import (
    analyse_modified_terminal, is_modified_terminal)
from ffcx.ir.uflacs.analysis.visualise import visualise_graph
from ffcx.ir.uflacs.elementtables import build_optimized_tables
from ufl.algorithms.balancing import balance_modifiers
from ufl.checks import is_cellwise_constant
from ufl.classes import CellCoordinate, FacetCoordinate, QuadratureWeight
from ufl.measure import facet_integral_types, point_integral_types
from ffcx.ir import dof_permutations

logger = logging.getLogger(__name__)

ma_data_t = collections.namedtuple("ma_data_t", ["ma_index", "tabledata"])

block_data_t = collections.namedtuple("block_data_t",
                                      ["ttypes",  # list of table types for each block rank
                                       "factor_indices_comp_indices",  # list of tuples (factor index, component index)
                                       "factor_is_piecewise",
                                       # bool: factor is found in piecewise vertex array
                                       # instead of quadloop specific vertex array
                                       "unames",  # list of unique FE table names for each block rank
                                       "restrictions",  # restriction "+" | "-" | None for each block rank
                                       "transposed",  # block is the transpose of another
                                       "is_uniform",
                                       "name",  # used in "preintegrated" and "premultiplied"
                                       "ma_data",  # used in "full", "safe" and "partial"
                                       "piecewise_ma_index",  # used in "partial"
                                       "is_permuted"  # Do quad points on facets need to be permuted?
                                       ])


def multiply_block_interior_facets(point_index, unames, ttypes, unique_tables,
                                   unique_table_num_dofs):
    rank = len(unames)
    tables = [unique_tables.get(name) for name in unames]
    num_dofs = tuple(unique_table_num_dofs[name] for name in unames)
    num_perms = tuple(t.shape[0] for t in tables)

    num_entities = max([1] + [tbl.shape[1] for tbl in tables if tbl is not None])
    ptable = numpy.zeros(num_perms + (num_entities, ) * rank + num_dofs)
    for perms in itertools.product(*[range(i) for i in num_perms]):
        for facets in itertools.product(*[range(num_entities)] * rank):
            vectors = []
            for i, tbl in enumerate(tables):
                if tbl is None:
                    assert ttypes[i] == "ones"
                    vectors.append(numpy.ones((num_dofs[i], )))
                else:
                    # Some tables are compacted along entities or points
                    e = 0 if tbl.shape[1] == 1 else facets[i]
                    q = 0 if tbl.shape[2] == 1 else point_index
                    vectors.append(tbl[perms[i], e, q, :])
            if rank > 1:
                assert rank == 2
                ptable[perms[0], perms[1], facets[0], facets[1], ...] = numpy.outer(*vectors)
            elif rank == 1:
                ptable[perms[0], facets[0], :] = vectors[0]
            else:
                raise RuntimeError("Nothing to multiply!")

    return ptable


def multiply_block(point_index, unames, ttypes, unique_tables, unique_table_num_dofs):
    rank = len(unames)
    tables = [unique_tables.get(name) for name in unames]
    num_perms = tuple(t.shape[0] for t in tables)
    num_dofs = tuple(unique_table_num_dofs[name] for name in unames)

    num_entities = max([1] + [tbl.shape[-3] for tbl in tables if tbl is not None])
    ptable = numpy.zeros(num_perms + (num_entities, ) + num_dofs)
    for perms in itertools.product(*[range(i) for i in num_perms]):
        for entity in range(num_entities):
            vectors = []
            for i, tbl in enumerate(tables):
                if tbl is None:
                    assert ttypes[i] == "ones"
                    vectors.append(numpy.ones((num_dofs[i], )))
                else:
                    # Some tables are compacted along entities or points
                    e = 0 if tbl.shape[1] == 1 else entity
                    q = 0 if tbl.shape[2] == 1 else point_index
                    vectors.append(tbl[perms[i], e, q, :])
            if rank > 1:
                ptable[perms[0], perms[1], entity, ...] = numpy.outer(*vectors)
            elif rank == 1:
                ptable[perms[0], entity, :] = vectors[0]
            else:
                raise RuntimeError("Nothing to multiply!")

    return ptable


def integrate_block(weights, unames, ttypes, unique_tables, unique_table_num_dofs):
    tables = [unique_tables.get(name) for name in unames]
    num_dofs = tuple(unique_table_num_dofs[name] for name in unames)
    num_perms = tuple(t.shape[0] for t in tables)

    num_entities = max([1] + [tbl.shape[-3] for tbl in tables if tbl is not None])
    ptable = numpy.zeros(num_perms + (num_entities, ) + num_dofs)
    for iq, w in enumerate(weights):
        ptable[...] += w * multiply_block(iq, unames, ttypes, unique_tables, unique_table_num_dofs)

    return ptable


def integrate_block_interior_facets(weights, unames, ttypes, unique_tables, unique_table_num_dofs):
    rank = len(unames)
    tables = [unique_tables.get(name) for name in unames]
    num_dofs = tuple(unique_table_num_dofs[name] for name in unames)
    num_perms = tuple(t.shape[0] for t in tables)

    num_entities = max([1] + [tbl.shape[-3] for tbl in tables if tbl is not None])
    ptable = numpy.zeros(num_perms + (num_entities, ) * rank + num_dofs)
    for iq, w in enumerate(weights):
        mtable = multiply_block_interior_facets(iq, unames, ttypes, unique_tables,
                                                unique_table_num_dofs)
        ptable[...] += w * mtable

    return ptable


def uflacs_default_parameters(optimize):
    """Default parameters for tuning of uflacs code generation.

    These are considered experimental and may change without deprecation
    mechanism at any time.
    """
    p = {
        # Relative precision to use when comparing finite element table
        # values for table reuse
        "table_rtol": 1e-6,

        # Absolute precision to use when comparing finite element table
        # values for table reuse and dropping of table zeros
        "table_atol": 1e-9,

        # Point chunk size for custom integrals
        "chunk_size": 8,

        # Code generation parameters
        "vectorize": False,
        "alignas": 32,
        "assume_aligned": None,
        "padlen": 1,
        "use_symbol_array": True
    }
    return p


def parse_uflacs_optimization_parameters(parameters, integral_type):
    """Extract parameters.
    Following model from quadrature representation, extracting
    uflacs specific parameters from the global parameters dict."""

    # Get default parameters
    p = uflacs_default_parameters(optimize=True)

    # Override with uflacs specific parameters if present in given
    # global parameters dict
    for key in p:
        if key in parameters:
            value = parameters[key]
            # Casting done here because main doesn't know about these
            # parameters
            if isinstance(p[key], int):
                value = int(value)
            elif isinstance(p[key], float):
                value = float(value)
            p[key] = value

    return p


def build_uflacs_ir(cell, integral_type, entitytype, integrands, argument_shape,
                    quadrature_rules, parameters, visualise):
    # The intermediate representation dict we're building and returning
    # here
    ir = {}

    # Extract uflacs specific optimization and code generation
    # parameters
    p = parse_uflacs_optimization_parameters(parameters, integral_type)

    # Pass on parameters for consumption in code generation
    ir["params"] = p

    # Shared unique tables for all quadrature loops
    ir["unique_tables"] = {}
    ir["unique_table_types"] = {}

    # Shared piecewise expr_ir for all quadrature loops
    ir["piecewise_ir"] = {"factorization": None,
                          "modified_arguments": [],
                          "block_contributions": collections.defaultdict(list)}

    # { num_points: expr_ir for one integrand }
    ir["varying_irs"] = {"factorization": None}

    # Whether we expect the quadrature weight to be applied or not (in
    # some cases it's just set to 1 in ufl integral scaling)
    tdim = cell.topological_dimension()
    expect_weight = (integral_type not in point_integral_types and (entitytype == "cell" or (
        entitytype == "facet" and tdim > 1) or (integral_type in ufl.custom_integral_types)))

    # Analyse each num_points/integrand separately
    assert isinstance(integrands, dict)
    all_num_points = sorted(integrands.keys())
    cases = [(num_points, [integrands[num_points]]) for num_points in all_num_points]
    ir["all_num_points"] = all_num_points

    ir["table_dofmaps"] = {}
    ir["table_dof_face_tangents"] = {}
    ir["table_dof_reflection_entities"] = {}

    for num_points, expressions in cases:

        assert len(expressions) == 1
        expression = expressions[0]

        # Rebalance order of nested terminal modifiers
        expression = balance_modifiers(expression)

        # Remove QuadratureWeight terminals from expression and replace with 1.0
        expression = replace_quadratureweight(expression)

        # Build initial scalar list-based graph representation
        S = build_scalar_graph(expression)

        # Build terminal_data from V here before factorization. Then we
        # can use it to derive table properties for all modified
        # terminals, and then use that to rebuild the scalar graph more
        # efficiently before argument factorization. We can build
        # terminal_data again after factorization if that's necessary.

        initial_terminals = {i: analyse_modified_terminal(v['expression'])
                             for i, v in S.nodes.items()
                             if is_modified_terminal(v['expression'])}

        (unique_tables, unique_table_types, unique_table_num_dofs,
         mt_unique_table_reference, table_origins) = build_optimized_tables(
            num_points,
            quadrature_rules,
            cell,
            integral_type,
            entitytype,
            initial_terminals.values(),
            ir["unique_tables"],
            rtol=p["table_rtol"],
            atol=p["table_atol"])

        for k, v in table_origins.items():
            ir["table_dof_face_tangents"][k] = dof_permutations.face_tangents(v[0])
            ir["table_dof_reflection_entities"][k] = dof_permutations.reflection_entities(v[0])

        for td in mt_unique_table_reference.values():
            ir["table_dofmaps"][td.name] = td.dofmap

        S_targets = [i for i, v in S.nodes.items() if v.get('target', False)]

        if 'zeros' in unique_table_types.values() and len(S_targets) == 1:
            # If there are any 'zero' tables, replace symbolically and rebuild graph
            #
            # TODO: Implement zero table elimination for non-scalar graphs
            for i, mt in initial_terminals.items():
                # Set modified terminals with zero tables to zero
                tr = mt_unique_table_reference.get(mt)
                if tr is not None and tr.ttype == "zeros":
                    S.nodes[i]['expression'] = ufl.as_ufl(0.0)

            # Propagate expression changes using dependency list
            for i, v in S.nodes.items():
                deps = [S.nodes[j]['expression'] for j in S.out_edges[i]]
                if deps:
                    v['expression'] = v['expression']._ufl_expr_reconstruct_(*deps)

            # Rebuild scalar target expressions and graph (this may be
            # overkill and possible to optimize away if it turns out to be
            # costly)
            expression = S.nodes[S_targets[0]]['expression']

            # Rebuild scalar list-based graph representation
            S = build_scalar_graph(expression)

        # Output diagnostic graph as pdf
        if visualise:
            visualise_graph(S, 'S.pdf')

        # Compute factorization of arguments
        rank = len(argument_shape)
        F = compute_argument_factorization(S, rank)

        # Get the 'target' nodes that are factors of arguments, and insert in dict
        FV_targets = [i for i, v in F.nodes.items() if v.get('target', False)]
        argument_factorization = {}

        for fi in FV_targets:
            # Number of blocks using this factor must agree with number of components
            # to which this factor contributes. I.e. there are more blocks iff there are more
            # components
            assert len(F.nodes[fi]['target']) == len(F.nodes[fi]['component'])

            k = 0
            for w in F.nodes[fi]['target']:
                comp = F.nodes[fi]['component'][k]
                argument_factorization[w] = argument_factorization.get(w, [])

                # Store tuple of (factor index, component index)
                argument_factorization[w].append((fi, comp))
                k += 1

        # Get list of indices in F which are the arguments (should be at start)
        argkeys = set()
        for w in argument_factorization:
            argkeys = argkeys | set(w)
        argkeys = list(argkeys)

        # Output diagnostic graph as pdf
        if visualise:
            visualise_graph(F, 'F.pdf')

        # Build set of modified_terminals for each mt factorized vertex in F
        # and attach tables, if appropriate
        for i, v in F.nodes.items():
            expr = v['expression']
            if is_modified_terminal(expr):
                mt = analyse_modified_terminal(expr)
                F.nodes[i]['mt'] = mt
                tr = mt_unique_table_reference.get(mt)
                if tr is not None:
                    F.nodes[i]['tr'] = tr

        # Attach 'status' to each node: 'inactive', 'piecewise' or 'varying'
        analyse_dependencies(F, mt_unique_table_reference)

        # Save the factorisation graph to the piecewise IR
        ir["piecewise_ir"]["factorization"] = F
        ir["piecewise_ir"]["modified_arguments"] = [F.nodes[i]['mt']
                                                    for i in argkeys]

        # Loop over factorization terms
        block_contributions = collections.defaultdict(list)
        for ma_indices, fi_ci in sorted(argument_factorization.items()):
            # Get a bunch of information about this term
            assert rank == len(ma_indices)
            trs = tuple(F.nodes[ai]['tr'] for ai in ma_indices)

            unames = tuple(tr.name for tr in trs)
            ttypes = tuple(tr.ttype for tr in trs)
            assert not any(tt == "zeros" for tt in ttypes)

            blockmap = tuple(tr.dofmap for tr in trs)
            block_is_uniform = all(tr.is_uniform for tr in trs)

            # Collect relevant restrictions to identify blocks correctly
            # in interior facet integrals
            block_restrictions = []
            for i, ai in enumerate(ma_indices):
                if trs[i].is_uniform:
                    r = None
                else:
                    r = F.nodes[ai]['mt'].restriction

                block_restrictions.append(r)
            block_restrictions = tuple(block_restrictions)

            # Check if each *each* factor corresponding to this argument is piecewise
            factor_is_piecewise = all(F.nodes[ifi[0]]["status"] == 'piecewise' for ifi in fi_ci)

            block_is_piecewise = factor_is_piecewise and not expect_weight
            block_is_permuted = False
            for n in unames:
                if unique_tables[n].shape[0] > 1:
                    block_is_permuted = True
            ma_data = []
            for i, ma in enumerate(ma_indices):
                if not trs[i].is_piecewise:
                    block_is_piecewise = False
                ma_data.append(ma_data_t(ma, trs[i]))

            block_is_transposed = False  # FIXME: Handle transposes for these block types

            # Add to contributions:
            # B[i] = sum_q weight * f * u[i] * v[j];  generated inside quadloop
            # A[blockmap] += B[i];                    generated after quadloop

            block_unames = unames
            blockdata = block_data_t(ttypes, fi_ci,
                                     factor_is_piecewise, block_unames,
                                     block_restrictions, block_is_transposed,
                                     block_is_uniform, None, tuple(ma_data), None, block_is_permuted)

            if block_is_piecewise:
                # Insert in piecewise expr_ir
                ir["piecewise_ir"]["block_contributions"][blockmap].append(blockdata)
            else:
                # Insert in varying expr_ir for this quadrature loop
                block_contributions[blockmap].append(blockdata)

        # Figure out which table names are referenced in unstructured
        # partition
        active_table_names = set()
        for i, v in F.nodes.items():
            tr = v.get('tr')
            if tr is not None and F.nodes[i]['status'] != 'inactive':
                active_table_names.add(tr.name)

        # Figure out which table names are referenced in blocks
        for blockmap, contributions in itertools.chain(
                block_contributions.items(), ir["piecewise_ir"]["block_contributions"].items()):
            for blockdata in contributions:
                for mad in blockdata.ma_data:
                    active_table_names.add(mad.tabledata.name)

        # Record all table types before dropping tables
        ir["unique_table_types"].update(unique_table_types)

        # Drop tables not referenced from modified terminals
        # and tables of zeros and ones
        unused_ttypes = ("zeros", "ones")
        keep_table_names = set()
        for name in active_table_names:
            ttype = ir["unique_table_types"][name]
            if ttype not in unused_ttypes:
                if name in unique_tables:
                    keep_table_names.add(name)
        unique_tables = {name: unique_tables[name] for name in keep_table_names}

        # Add to global set of all tables
        for name, table in unique_tables.items():
            tbl = ir["unique_tables"].get(name)
            if tbl is not None and not numpy.allclose(
                    tbl, table, rtol=p["table_rtol"], atol=p["table_atol"]):
                raise RuntimeError("Table values mismatch with same name.")
        ir["unique_tables"].update(unique_tables)

        # Analyse active terminals to check what we'll need to generate code for
        active_mts = []
        for i, v in F.nodes.items():
            mt = v.get('mt', False)
            if mt and F.nodes[i]['status'] != 'inactive':
                active_mts.append(mt)

        # Figure out if we need to access CellCoordinate to avoid
        # generating quadrature point table otherwise
        if integral_type == "cell":
            need_points = any(isinstance(mt.terminal, CellCoordinate) for mt in active_mts)
        elif integral_type in facet_integral_types:
            need_points = any(isinstance(mt.terminal, FacetCoordinate) for mt in active_mts)
        elif integral_type in ufl.custom_integral_types:
            need_points = True  # TODO: Always?
        elif integral_type == "expression":
            need_points = True
        else:
            need_points = False

        # Figure out if we need to access QuadratureWeight to avoid
        # generating quadrature point table otherwise need_weights =
        # any(isinstance(mt.terminal, QuadratureWeight) for mt in
        # active_mts)

        if expect_weight:
            need_weights = True
        elif integral_type in ufl.custom_integral_types:
            need_weights = True  # TODO: Always?
        elif integral_type == "expression":
            need_weights = True
        else:
            need_weights = False

        # Build IR dict for the given expressions
        # Store final ir for this num_points
        ir["varying_irs"][num_points] = {"factorization": F,
                                         "modified_arguments": [F.nodes[i]['mt'] for i in argkeys],
                                         "block_contributions": block_contributions,
                                         "need_points": need_points,
                                         "need_weights": need_weights}
    return ir


def analyse_dependencies(F, mt_unique_table_reference):
    # Sets 'status' of all nodes to either: 'inactive', 'piecewise' or 'varying'
    # Children of 'target' nodes are either 'piecewise' or 'varying'.
    # All other nodes are 'inactive'.
    # Varying nodes are identified by their tables ('tr'). All their parent
    # nodes are also set to 'varying' - any remaining active nodes are 'piecewise'.

    # Set targets, and dependencies to 'active'
    targets = [i for i, v in F.nodes.items() if v.get('target')]
    for i, v in F.nodes.items():
        v['status'] = 'inactive'

    while targets:
        s = targets.pop()
        F.nodes[s]['status'] = 'active'
        for j in F.out_edges[s]:
            if F.nodes[j]['status'] == 'inactive':
                targets.append(j)

    # Build piecewise/varying markers for factorized_vertices
    varying_ttypes = ("varying", "quadrature", "uniform")
    varying_indices = []
    for i, v in F.nodes.items():
        if v.get('mt') is None:
            continue
        tr = v.get('tr')
        if tr is not None:
            ttype = tr.ttype
            # Check if table computations have revealed values varying over points
            if ttype in varying_ttypes:
                varying_indices.append(i)
            else:
                if ttype not in ("fixed", "piecewise", "ones", "zeros"):
                    raise RuntimeError("Invalid ttype %s" % (ttype, ))

        elif not is_cellwise_constant(v['expression']):
            raise RuntimeError("Error")
            # Keeping this check to be on the safe side,
            # not sure which cases this will cover (if any)
            # varying_indices.append(i)

    # Set all parents of active varying nodes to 'varying'
    while varying_indices:
        s = varying_indices.pop()
        if F.nodes[s]['status'] == 'active':
            F.nodes[s]['status'] = 'varying'
            for j in F.in_edges[s]:
                varying_indices.append(j)

    # Any remaining active nodes must be 'piecewise'
    for i, v in F.nodes.items():
        if v['status'] == 'active':
            v['status'] = 'piecewise'


def replace_quadratureweight(expression):
    """Remove any QuadratureWeight terminals and replace with 1.0."""

    r = _find_terminals_in_ufl_expression(expression, QuadratureWeight)
    replace_map = {q: 1.0 for q in r}

    return ufl.algorithms.replace(expression, replace_map)


def _find_terminals_in_ufl_expression(e, etype):
    """Recursively search expression for terminals of type etype."""
    r = []
    for op in e.ufl_operands:
        if is_modified_terminal(op) and isinstance(op, etype):
            r.append(op)
        else:
            r += _find_terminals_in_ufl_expression(op, etype)

    return r