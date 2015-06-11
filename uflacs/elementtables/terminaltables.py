# -*- coding: utf-8 -*-
# Copyright (C) 2011-2015 Martin Sandve Alnæs
#
# This file is part of UFLACS.
#
# UFLACS is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# UFLACS is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with UFLACS. If not, see <http://www.gnu.org/licenses/>.

"""Tools for precomputed tables of terminal values."""

from six import iteritems, iterkeys
from six.moves import xrange as range

import ufl
from ufl.common import product
from ufl.utils.derivativetuples import derivative_listing_to_counts
from ufl.classes import FormArgument, GeometricQuantity, SpatialCoordinate, Jacobian
from ufl.algorithms.analysis import unique_tuple

from ffc.log import ffc_assert

from uflacs.datastructures.arrays import object_array
from uflacs.elementtables.table_utils import (generate_psi_table_name,
                                              get_ffc_table_values,
                                              strip_table_zeros,
                                              build_unique_tables)

def extract_terminal_elements(terminal_data):
    "Extract a list of unique elements from terminal data."
    elements = []
    xs = {}
    for mt in terminal_data:
        t = mt.terminal
        if isinstance(t, FormArgument):
            # Add element for function and its coordinates
            elements.append(t.domain().coordinate_element())
            elements.append(t.element())

        elif isinstance(t, GeometricQuantity):
            # Add element for coordinate field of domain
            elements.append(t.domain().coordinate_element())

    return unique_tuple(elements)


def build_element_counter_map(elements):
    "Given a sequence of elements, build a unique mapping: element->int."
    element_counter_map = {}
    for element in sorted(elements):  # TODO: Stable sorting?
        if element not in element_counter_map:
            element_counter_map[element] = len(element_counter_map)
    return element_counter_map


def build_element_tables(psi_tables, num_points, entitytype, terminal_data):
    """Build the element tables needed for a list of modified terminals.

    Concepts:


    Input:
      psi_tables
      entitytype
      terminal_data

    New output:
      tables
      terminal_table_names
    """
    element_counter_map = {}  # build_element_counter_map(extract_terminal_elements(terminal_data))
    terminal_table_names = object_array(len(terminal_data))
    tables = {}
    for i, mt in enumerate(terminal_data):
        t = mt.terminal
        rv = mt.reference_value
        gd = mt.global_derivatives
        ld = mt.local_derivatives
        gc = mt.component
        fc = mt.flat_component

        if gd and rv:
            error("Global derivatives of reference values not defined.")
        if ld and not rv:
            import IPython; IPython.embed()
            error("Local derivatives of global values not defined.")

        domain = t.domain()

        # Add to element tables for FormArguments and relevant GeometricQuantities
        if isinstance(t, FormArgument):
            element = t.element()

        elif isinstance(t, SpatialCoordinate):
            element = domain.coordinate_element()

        elif isinstance(t, Jacobian):
            element = domain.coordinate_element()
            fc = gc[0]
            ld = tuple(sorted((gc[1],) + ld))
            #fc, ld = gc
            #ld = (ld,)
        else:
            continue

        # Count elements as we go
        element_counter = element_counter_map.get(element)
        if element_counter is None:
            element_counter = len(element_counter_map)
            element_counter_map[element] = element_counter

        # Change derivatives format for table lookup
        gdim = domain.geometric_dimension()
        tdim = domain.topological_dimension()
        global_derivatives = tuple(derivative_listing_to_counts(gd, gdim))
        local_derivatives = tuple(derivative_listing_to_counts(ld, tdim))

        # Build name for this particular table
        name = generate_psi_table_name(element_counter, fc,
                                       local_derivatives, mt.averaged, entitytype, num_points)

        # Extract the values of the table from ffc table format
        table = tables.get(name)
        if table is None:
            table = get_ffc_table_values(psi_tables, entitytype, num_points,
                                         element, fc, local_derivatives)
            tables[name] = table

        # Store table name with modified terminal
        terminal_table_names[i] = name

    return tables, terminal_table_names


def optimize_element_tables(tables, terminal_table_names):
    """Optimize tables.

    Input:
      tables - a mapping from name to table values
      terminal_table_names - a list of table names

    Output:
      unique_tables_dict - a new and mapping from name to table values with stripped zero columns
      terminal_table_ranges - a list of (table name, begin, end) for each of the input table names
    """

    # Names here are a bit long and slightly messy...

    # Apply zero stripping to all tables
    stripped_tables = {}
    table_ranges = {}
    for name, table in iteritems(tables):
        begin, end, stripped_table = strip_table_zeros(table)
        stripped_tables[name] = stripped_table
        table_ranges[name] = (begin, end)

    # Build unique table mapping
    unique_tables_list, table_name_to_unique_index = build_unique_tables(stripped_tables)

    # Build mapping of constructed table names to unique names,
    # pick first constructed name
    unique_table_names = {}
    for name in sorted(iterkeys(table_name_to_unique_index)):
        unique_index = table_name_to_unique_index[name]
        if unique_index in unique_table_names:
            continue
        unique_table_names[unique_index] = name

    # Build mapping from unique table name to the table itself
    unique_tables = dict((unique_table_names[unique_index], unique_tables_list[unique_index])
                         for unique_index in range(len(unique_tables_list)))

    # Build mapping from terminal data index to compacted table data:
    # terminal data index -> (unique name, table range begin, table range end)
    terminal_table_ranges = object_array(len(terminal_table_names))
    for i, name in enumerate(terminal_table_names):
        if name is not None:
            unique_index = table_name_to_unique_index[name]
            unique_name = unique_table_names[unique_index]
            b, e = table_ranges[name]
            terminal_table_ranges[i] = (unique_name, b, e)

    return unique_tables, terminal_table_ranges

# TODO: This seems to be unused, remove?
def generate_element_table_definitions(L, tables):
    "Format a dict of name->table into code."
    code = []
    for name in sorted(tables):
        table = tables[name]
        if product(table.shape) > 0:
            code += [L.ArrayDecl("static const double",
                                 name, table.shape, table)]
    return code
