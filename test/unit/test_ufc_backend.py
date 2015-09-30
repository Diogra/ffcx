

from uflacs.backends.ufc.generators import *

import uflacs.language.cnodes as L


# TODO: Make this a feature of dijitso: dijitso show-function modulehash functionname
def extract_function(name, code):
    lines = code.split("\n")
    n = len(lines)
    begin = None
    body = None
    for i in range(n):
        if (name+"(") in lines[i]:
            for j in range(i, n):
                if lines[j] == "{":
                    begin = i
                    body = j
                    break
            break
    if begin is None:
        return "didnt find %s" % (name,)
    end = n
    for i in range(body, n):
        if lines[i] == "}":
            end = i+1
            break
    sublines = lines[begin:end]
    return '\n'.join(sublines)


def basic_class_properties(classname):
    ir = {
        "classname": classname,
        "constructor": "",
        "constructor_arguments": "",
        "initializer_list": "",
        "destructor": "",
        "members": "",
        "preamble": "",
        }
    return ir

def mock_form_ir():
    ir = basic_class_properties("mock_form_classname")
    ir.update({
        "signature": "mock form signature",
        "rank": 2,
        "num_coefficients": 3,
        "original_coefficient_positions": [0, 2],
        })

    ir.update({
        "create_coordinate_finite_element": "mock_coordinate_finite_element_classname",
        "create_coordinate_dofmap": "mock_coordinate_dofmap_classname",
        "create_finite_element": ["mock_finite_element_classname_%d" % (i,) for i in range(ir["num_coefficients"])],
        "create_dofmap": ["mock_dofmap_classname_%d" % (i,) for i in range(ir["num_coefficients"])],
        })

    # These are the method names in ufc::form that are specialized for each integral type
    template = "max_%s_subdomain_id"
    for i, integral_type in enumerate(ufc_integral_types):
        key = template % integral_type
        ir[key] = i # just faking some integers

    template = "has_%s_integrals"
    for i, integral_type in enumerate(ufc_integral_types):
        key = template % integral_type
        ir[key] = (i % 2 == 0) # faking some bools

    template = "create_%s_integral"
    for i, integral_type in enumerate(ufc_integral_types):
        key = template % integral_type
        ir[key] = [key.replace("create_", "") + str(j) for j in range(i)] # faking list of classnames

    template = "create_default_%s_integral"
    for i, integral_type in enumerate(ufc_integral_types):
        key = template % integral_type
        ir[key] = key.replace("create_", "") # faking classname

    return ir

def mock_dofmap_ir():
    ir = basic_class_properties("mock_dofmap_classname")
    ir.update({
        "signature": "mock element signature",
        "geometric_dimension": 3,
        "topological_dimension": 2,
        "global_dimension": "    mock global_dimension body();",
        "needs_mesh_entities": [True, False, True],
        "num_element_dofs": 7,
        "num_entity_dofs": [3,0,1],
        "num_facet_dofs": 7,
        "num_sub_dofmaps": 3,
        "create_sub_dofmap": ["mock_dofmap_classname_sub_%d" % (i,) for i in range(3)],
        })
    return ir

def mock_finite_element_ir():
    ir = basic_class_properties("mock_finite_element_classname")
    ir.update({
        "signature": "mock element signature",
        "cell_shape": "mock_cell_shape",
        "geometric_dimension": 3,
        "topological_dimension": 2,
        "value_dimension": (3,3),
        "reference_value_dimension": (2,2),
        "space_dimension": 6,
        "tabulate_dof_coordinates": [(0.0, 0.0), (0.0, 1.0), (1.0, 0.0)],
        "evaluate_basis": "fixme",
        "evaluate_basis_derivatives": "fixme",
        "evaluate_basis_all": "fixme",
        "evaluate_basis_derivatives_all": "fixme",
        "evaluate_dof": "fixme",
        "evaluate_dofs": "fixme",
        "interpolate_vertex_values": "fixme",
        "num_sub_elements": 3,
        "create_sub_element": ["mock_finite_element_classname_sub_%d" % (i,) for i in range(3)],
        })
    return ir

def mock_integral_ir():
    ir = basic_class_properties("mock_integral_classname")
    ir.update({
        "enabled_coefficients": [True, False, True],
        "tabulate_tensor": "    mock_body_of_tabulate_tensor();",
        "num_cells": 1,
        })
    return ir

def mock_domain_ir():
    ir = basic_class_properties("mock_domain_classname")
    ir.update({
        "signature": "mock_domain_signature",
        "cell_shape": "mock_cell_shape",
        "geometric_dimension": 3,
        "topological_dimension": 2,
        "create_coordinate_finite_element": "mock_coordinate_finite_element_classname",
        "create_coordinate_dofmap": "mock_coordinate_dofmap_classname",
        })
    return ir




def compile_mock_domain():
    ir = mock_domain_ir()
    gen = ufc_domain()
    return gen.generate(L, ir)

def compile_mock_form():
    ir = mock_form_ir()
    gen = ufc_form()
    return gen.generate(L, ir)

def compile_mock_dofmap():
    ir = mock_dofmap_ir()
    gen = ufc_dofmap()
    return gen.generate(L, ir)

def compile_mock_finite_element():
    ir = mock_finite_element_ir()
    gen = ufc_finite_element()
    return gen.generate(L, ir)

def compile_mock_integral(integral_type):
    ir = mock_integral_ir()
    gen = eval("ufc_%s_integral" % integral_type)()
    return gen.generate(L, ir)

def compile_mock_all():
    mocks = [compile_mock_integral(integral_type) for integral_type in ufc_integral_types]
    mocks += [compile_mock_form(), compile_mock_dofmap(), compile_mock_finite_element()]
    return '\n\n'.join(mocks)


def test_mock_domain():
    h, cpp = compile_mock_domain()
    print h
    print cpp

def test_mock_form():
    h, cpp = compile_mock_form()
    print h
    print cpp

def test_mock_dofmap():
    h, cpp = compile_mock_dofmap()
    print h
    print cpp

def test_mock_finite_element():
    h, cpp = compile_mock_finite_element()
    print h
    print cpp

def test_mock_integral():
    for integral_type in ufc_integral_types:
        h, cpp = compile_mock_integral(integral_type)
        print h
        print cpp

def test_foo():
    ir = mock_form_ir()
    print ufc_form.create_cell_integral.__doc__
    print ufc_form().create_cell_integral(L, ir)

def test_mock_extract_function():
    h, cpp = compile_mock_integral("cell")
    name = "enabled_coefficients"
    print
    print
    print name
    print extract_function(name, cpp)
    print

"""Missing:
form:
original_coefficient_position

dofmap:
global_dimension
tabulate_dofs
tabulate_facet_dofs
tabulate_entity_dofs

finite_element:
evaluate_basis*
evaluate_dof
interpolate_vertex_values
tabulate_dof_coordinates

integrals:
tabulate_tensor

all:
everything with classnames
"""
