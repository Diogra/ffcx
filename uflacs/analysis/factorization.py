
from six import itervalues, iterkeys, iteritems
from six.moves import xrange as range
from ufl import as_ufl
from ufl.classes import Terminal, Indexed, Grad, Restricted, FacetAvg, CellAvg, Argument, Product, Sum, Division

from ffc.log import ffc_assert

from uflacs.datastructures.arrays import int_array, object_array
from uflacs.analysis.graph_dependencies import compute_dependencies
from uflacs.analysis.modified_terminals import analyse_modified_terminal, strip_modified_terminal

def _build_arg_sets(V):
    "Build arg_sets = { argument number: set(j for j where V[j] is a modified Argument with this number) }"
    arg_sets = {}
    for i, v in enumerate(V):
        arg = strip_modified_terminal(v)
        if not isinstance(arg, Argument):
            continue
        num = arg.number()
        arg_set = arg_sets.get(num)
        if arg_set is None:
            arg_set = {}
            arg_sets[num] = arg_set
        arg_set[i] = v
    return arg_sets

def _build_argument_indices_from_arg_sets(V, arg_sets):
    "Build ordered list of indices to modified arguments."
    # Build set of all indices of V referring to modified arguments
    arg_indices = set()
    for js in itervalues(arg_sets):
        arg_indices.update(js)

    # Make a canonical ordering of vertex indices for modified arguments
    def arg_ordering_key(i):
        "Return a key for sorting argument vertex indices based on the properties of the modified terminal."
        mt = analyse_modified_terminal(arg_ordering_key.V[i])
        assert isinstance(mt.terminal, Argument)
        assert mt.terminal.number() >= 0
        return (mt.terminal.number(), mt.terminal.part(),
                mt.component,
                mt.global_derivatives, mt.local_derivatives,
                mt.restriction, mt.averaged)
    arg_ordering_key.V = V
    ordered_arg_indices = sorted(arg_indices, key=arg_ordering_key)

    return ordered_arg_indices

def build_argument_indices(V):
    "Build ordered list of indices to modified arguments."
    arg_sets = _build_arg_sets(V)
    ordered_arg_indices = _build_argument_indices_from_arg_sets(V, arg_sets)
    return ordered_arg_indices

def build_argument_dependencies(dependencies, arg_indices):
    "Preliminary algorithm: build list of argument vertex indices each vertex (indirectly) depends on."
    n = len(dependencies)
    A = [[] for i in range(n)] # TODO: Use array
    for i, deps in enumerate(dependencies):
        argdeps = []
        for j in deps:
            if j in arg_indices:
                argdeps.append(j)
            else:
                argdeps.extend(A[j])
        A[i] = sorted(argdeps)
    return A



def add_to_fv(expr, FV, e2fi):
    fi = e2fi.get(expr)
    if fi is None:
        fi = len(e2fi)
        FV.append(expr)
        e2fi[expr] = fi
    return fi

# Reuse these empty objects where appropriate to save memory
noargs = {}

def handle_modified_terminal(i, v, F, FV, e2fi, arg_indices, AV, sv2av):
    # v is a modified terminal...
    if i in arg_indices:
        # ... a modified Argument
        argkey = (i,)
        fi = None

        # Adding 1 as an expression allows avoiding special representation by representing "v" as "1*v"
        one = add_to_fv(as_ufl(1.0), FV, e2fi)
        factors = { argkey: one }

        assert AV[sv2av[i]] == v
    else:
        # ... record a non-argument modified terminal
        factors = noargs
        fi = add_to_fv(v, FV, e2fi)
    return fi, factors

def handle_sum(i, v, deps, F, FV, sv2fv, e2fi):
    ffc_assert(len(deps) == 2, "Assuming binary sum here. This can be fixed if needed.")
    fac0 = F[deps[0]]
    fac1 = F[deps[1]]

    # This assertion would fail for combined matrix+vector factorization
    if 0 and len(fac0) != len(fac1):
        print('\n'*5)
        print(i, deps)
        print(str(v))
        print(repr(v))
        print(str(v.operands()[0]))
        print(str(v.operands()[1]))
        print(fac0)
        print(fac1)
        print('\n'*5)

    argkeys = sorted(set(iterkeys(fac0)) | set(iterkeys(fac1)))

    if argkeys: # f*arg + g*arg = (f+g)*arg
        keylen = len(argkeys[0])
        fi = None
        factors = {}
        for argkey in argkeys:
            ffc_assert(len(argkey) == keylen, "Expecting equal argument rank terms among summands.")

            fi0 = fac0.get(argkey)
            fi1 = fac1.get(argkey)
            if fi0 is None:
                fisum = fi1
            elif fi1 is None:
                fisum = fi0
            else:
                f0 = FV[fi0]
                f1 = FV[fi1]
                fisum = add_to_fv(f0 + f1, FV, e2fi)
            factors[argkey] = fisum

    else: # non-arg + non-arg
        factors = noargs
        fi = add_to_fv(v, FV, e2fi)

    return fi, factors

def handle_product(i, v, deps, F, FV, sv2fv, e2fi):
    ffc_assert(len(deps) == 2, "Assuming binary product here. This can be fixed if needed.")
    fac0 = F[deps[0]]
    fac1 = F[deps[1]]

    if not fac0 and not fac1: # non-arg * non-arg
        # Record non-argument product
        factors = noargs
        f0 = FV[sv2fv[deps[0]]]
        f1 = FV[sv2fv[deps[1]]]
        assert f1*f0 == v
        fi = add_to_fv(v, FV, e2fi)
        assert FV[fi] == v
        if 0:
            print("NON*NON:", i, str(v))
            print("        ", fi)
            print("        ", factors)

    elif not fac0: # non-arg * arg
        f0 = FV[sv2fv[deps[0]]]
        fi = None
        factors = {}
        for k1, fi1 in iteritems(fac1):
            # Record products of non-arg operand with each factor of arg-dependent operand
            factors[k1] = add_to_fv(f0*FV[fi1], FV, e2fi)
        if 0:
            print("NON*ARG:", i, str(v))
            print("        ", factors)

    elif not fac1: # arg * non-arg
        f1 = FV[sv2fv[deps[1]]]
        fi = None
        factors = {}
        for k0, fi0 in iteritems(fac0):
            # Record products of non-arg operand with each factor of arg-dependent operand
            factors[k0] = add_to_fv(f1*FV[fi0], FV, e2fi)
        if 0:
            print("ARG*NON:", i, str(v))
            print("        ", factors)

    else: # arg * arg
        fi = None
        factors = {}
        for k0, fi0 in iteritems(fac0):
            for k1, fi1 in iteritems(fac1):
                # Record products of each factor of arg-dependent operand
                argkey = tuple(sorted(k0+k1)) # sort key for canonical representation
                factors[argkey] = add_to_fv(FV[fi0]*FV[fi1], FV, e2fi)
        if 0:
            print("ARG*ARG:", i, str(v))
            print("        ", factors)
    return fi, factors

def handle_division(i, v, deps, F, FV, sv2fv, e2fi):
    fac0 = F[deps[0]]
    fac1 = F[deps[1]]
    assert not fac1, "Cannot divide by arguments."

    if fac0: # arg / non-arg
        f1 = FV[sv2fv[deps[1]]]
        fi = None
        factors = {}
        for k0, fi0 in iteritems(fac0):
            # Record products of non-arg operand with each factor of arg-dependent operand
            factors[k0] = add_to_fv(FV[fi0] / f1, FV, e2fi)

    else: # non-arg / non-arg
        # Record non-argument subexpression
        fi = add_to_fv(v, FV, e2fi)
        factors = noargs

    return fi, factors

def handle_operator(i, v, deps, F, FV, sv2fv, e2fi):
    # TODO: Check something?
    facs = [F[deps[j]] for j in range(len(deps))]
    if any(facs):
        # TODO: Can this happen?
        error("Assuming that a {0} cannot be applied to arguments. If this is wrong please report a bug..".format(type(v)))
    else:
        # Record non-argument subexpression
        fi = add_to_fv(v, FV, e2fi)
        factors = noargs
    return fi, factors

def collect_argument_factors(SV, dependencies, arg_indices):
    """Factorizes a scalar expression graph w.r.t. scalar Argument
    components.

    The result is a triplet (AV, FV, IM):

      - The scalar argument component subgraph:

          AV[ai] = v

        with the property

          SV[arg_indices] == AV[:]

      - An expression graph vertex list with all non-argument factors:

          FV[fi] = f

        with the property that none of the expressions depend on Arguments.

      - A dict representation of the final integrand of rank r:

          IM = { (ai1_1, ..., ai1_r): fi1, (ai2_1, ..., ai2_r): fi2, }

        This mapping represents the factorization of SV[-1] w.r.t. Arguments s.t.:

          SV[-1] := sum(FV[fik] * product(AV[j] for j in aik) for aik, fik in IM.items())

        where := means equivalence in the mathematical sense,
        of course in a different technical representation.

    """
    # TODO: What did this comment refer to? It's probably deprecated now:
    # Instead of argdeps being a list of argument vertex indices v (indirectly) depends on,
    # it should be a mapping { combo: factors } to handle e.g. (u + fu')(gv + v')

    # Extract argument component subgraph
    AV = [SV[j] for j in arg_indices]
    av2sv = arg_indices
    sv2av = dict( (j, i) for i, j in enumerate(arg_indices) )
    assert all(AV[i] == SV[j] for i, j in enumerate(arg_indices))
    assert all(AV[i] == SV[j] for j, i in iteritems(sv2av))

    # Data structure for building non-argument factors
    FV = []
    e2fi = {}

    # Hack to later build dependencies for the FV entries that change K*K -> K**2
    two = add_to_fv(as_ufl(2), FV, e2fi)

    # Intermediate factorization for each vertex in SV on the format
    # F[i] = None # if SV[i] does not depend on arguments
    # F[i] = { argkey: fi } # if SV[i] does depend on arguments, where:
    #   FV[fi] is the expression SV[i] with arguments factored out
    #   argkey is a tuple with indices into SV for each of the argument components SV[i] depends on
    # F[i] = { argkey1: fi1, argkey2: fi2, ... } # if SV[i] is a linear combination of multiple argkey configurations
    F = object_array(len(SV)) # TODO: Use some CRS based format?
    sv2fv = int_array(len(SV))

    # Factorize each subexpression in order:
    for i, v in enumerate(SV):
        deps = dependencies[i]

        if not len(deps):
            fi, factors = handle_modified_terminal(i, v, F, FV, e2fi, arg_indices, AV, sv2av)
        elif isinstance(v, Sum):
            fi, factors = handle_sum(i, v, deps, F, FV, sv2fv, e2fi)
        elif isinstance(v, Product):
            fi, factors = handle_product(i, v, deps, F, FV, sv2fv, e2fi)
        elif isinstance(v, Division):
            fi, factors = handle_division(i, v, deps, F, FV, sv2fv, e2fi)
        else: # All other operators
            fi, factors = handle_operator(i, v, deps, F, FV, sv2fv, e2fi)

        #print 'fac:', i, factors
        if fi is not None:
            sv2fv[i] = fi
        F[i] = factors

    assert not noargs, "This dict was not supposed to be filled with anything!"

    # Throw away superfluous items in array
    #FV = FV[:len(e2fi)]
    assert len(FV) == len(e2fi)

    # Get the factorization of the final value # TODO: Support simultaneous factorization of multiple integrands?
    IM = F[-1]

    # Map argkeys from indices into SV to indices into AV, and resort keys for canonical representation
    IM = dict( (tuple(sorted(sv2av[j] for j in argkey)), fi) for argkey, fi in iteritems(IM) )

    # If this is a non-argument expression, point to the expression from IM (not sure if this is useful)
    if any([not AV, not IM, not arg_indices]):
        assert all([not AV, not IM, not arg_indices])
        IM = { (): len(FV)-1 }

    return FV, e2fi, AV, IM

def rebuild_scalar_graph_from_factorization(AV, FV, IM):
    # TODO: What about multiple target_variables?

    # Build initial graph
    SV = []
    SV.extend(AV)
    SV.extend(FV)
    se2i = dict( (s, i) for i, s in enumerate(SV) )

    def add_vertex(h):
        # Avoid adding vertices twice
        i = se2i.get(h)
        if i is None:
            se2i[h] = len(SV)
            SV.append(h)

    # Add factorization monomials
    argkeys = sorted(iterkeys(IM))
    fs = []
    for argkey in argkeys:
        # Start with coefficients
        f = FV[IM[argkey]]
        ###f = 1

        # Add binary products with each argument in order
        for argindex in argkey:
            f = f*AV[argindex]
            add_vertex(f)

        # Add product with coefficients last
        ###f = f*FV[IM[argkey]]
        ###add_vertex(f)

        # f is now the full monomial, store it as a term for sum below
        fs.append(f)

    # Add sum of factorization monomials
    g = 0
    for f in fs:
        g = g + f
        add_vertex(g)

    # Rebuild dependencies
    dependencies = compute_dependencies(se2i, SV)

    if 0:
        print('\n'*10)
        print('AV:')
        print('\n'.join('  {}: {}'.format(i, s) for i, s in enumerate(AV)))
        print('FV:')
        print('\n'.join('  {}: {}'.format(i, s) for i, s in enumerate(FV)))
        print('IM:')
        print('\n'.join('  {}: {}'.format(i, IM[i]) for i in sorted(iterkeys(IM))))
        print('SV:')
        print('\n'.join('  {}: {}'.format(i, s) for i, s in enumerate(SV)))
        print('\n'*10)

    return SV, se2i, dependencies

def compute_argument_factorization(SV, target_variables, dependencies):

    # TODO: Use target_variables! Currently just assuming the last vertex is the target here...

    if list(target_variables) != [len(SV)-1]:
        ffc_assert(not extract_type(SV[-1], Argument),
                      "Multiple or nonscalar Argument dependent expressions not supported in factorization.")
        AV = []
        FV = SV
        IM = {}
        return AV, FV, IM, target_variables, dependencies

    assert list(target_variables) == [len(SV)-1]

    arg_indices = build_argument_indices(SV)
    A = build_argument_dependencies(dependencies, arg_indices)
    FV, e2fi, AV, IM = collect_argument_factors(SV, dependencies, arg_indices)

    # Indices into FV that are needed for final result
    target_variables = sorted(itervalues(IM))

    dependencies = compute_dependencies(e2fi, FV)

    return IM, AV, FV, target_variables, dependencies
