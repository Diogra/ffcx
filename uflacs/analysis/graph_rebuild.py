
from itertools import izip
from ufl.common import product
from ufl.permutation import compute_indices

import ufl
from ufl import as_vector
from ufl.classes import (MultiIndex, ComponentTensor, ListTensor, Transposed, Variable,
                         IndexSum, UtilityType, Label, Data)
from ufl.algorithms import MultiFunction

from uflacs.utils.log import error, uflacs_assert
from uflacs.analysis.datastructures import (int_array, object_array,
                                              CRS, rows_to_crs, rows_dict_to_crs)
from uflacs.analysis.indexing import indexing_to_component

from uflacs.analysis.modified_terminals import is_modified_terminal

class ReconstructScalarSubexpressions(MultiFunction):
    def __init__(self):
        super(ReconstructScalarSubexpressions, self).__init__()

    def expr(self, o, *args, **kwargs):
        error("No handler for type %s" % type(o))

    def terminal(self, o):
        error("Not expecting terminal expression in here, got %s." % type(o))

    def scalar_nary(self, o, ops):
        uflacs_assert(o.shape() == (), "Expecting scalar.")
        sops = [op[0] for op in ops]
        return [o.reconstruct(*sops)]

    # Unary scalar functions
    math_function = scalar_nary
    abs = scalar_nary
    # Binary scalar functions
    power = scalar_nary
    bessel_function = scalar_nary # TODO: Is this ok?

    def element_wise(self, scalar_operator, o, ops):
        # FIXME FIXME FIXME: products like A[i,j]*B[j,k] are allowed,
        # which means that indices do not line up and the total shape
        # of operands are different, not just 1/n like below...

        # Products of a scalar and a tensor are allowed
        n = max(len(op) for op in ops)
        uflacs_assert(all(len(op) in (1,n) for op in ops), "Unexpected number of operands.")
        # Compute each scalar value
        res = []
        for k in xrange(n):
            sops = []
            for op in ops:
                if len(op) == 1:
                    sops.append(op[0])
                else:
                    uflacs_assert(len(op) == n, "Expecting n operands.")
                    sops.append(op[k])
            res.append(scalar_operator(sops))
        return res

    def element_wise2(self, scalar_operator, o, ops):
        # FIXME FIXME FIXME: products like A[i,j]*B[j,k] are allowed,
        # which means that indices do not line up and the total shape
        # of operands are different, not just 1/n like below...

        # Compute shapes and sizes of o
        # Regular shape
        sh = o.shape()
        m = product(sh)
        # Index shape
        ii = sorted(o.free_indices(), key=lambda x: x.count())
        idims = o.index_dimensions()
        ish = tuple(idims[i] for i in ii)
        im = product(ish)
        # Total shape
        tsh = sh+ish
        tm = m*im
        uflacs_assert(product(tsh) == tm, "Inconsistent shapes and sizes computed.")

        if 0:
            print
            print 'ii', ii
            print 'idims', idims
            print 'sh ', sh, m
            print 'ish', ish, im
            print 'tsh', tsh, tm
            print

        # Check that we have at most one tensor shaped operand here
        if sh == ():
            uflacs_assert(all(op.shape() == () for op in o.operands()),
                          "Expecting scalars.")
        else:
            shaped = 0
            for j,op in enumerate(o.operands()):
                opsh = op.shape()
                if opsh == ():
                    continue
                elif opsh == sh:
                    shaped += 1
                else:
                    error("Not expecting shape %s, overall shape is %s." % (opsh, sh))
            uflacs_assert(shaped in (0,1), "Expecting at most one shaped operand.")

        # Precompute some dimensions for each operand
        istrides = [None]*len(ops)
        opims = [None]*len(ops)
        for j,op in enumerate(o.operands()):
            opii = sorted(op.free_indices(), key=lambda x: x.count())
            opidims = op.index_dimensions()
            opish = tuple(opidims[i] for i in opii)
            opim = product(opish)
            opsh = op.shape()
            opm = product(opsh)
            optm = opm*opim
            optsh = opsh+opish
            uflacs_assert(product(optsh) == optm, "Mismatching shapes and sizes.")

            running = 1
            strides = []
            for globi in reversed(ii):
                d = opidims.get(globi)
                if d is None:
                    strides.append(0)
                else:
                    loci = opii.index(globi)
                    strides.append(running)
                    running *= d
            #uflacs_assert(strides[-1] == opim, "Invalid stride.")
            istrides[j] = tuple(reversed(strides))
            opims[j] = opim if opsh else 0

        if 0:
            print
            print istrides
            print opims
            print

        # Compute each scalar value
        res = []
        for sc in compute_indices(sh): # TODO: Optimization: swap loops so we recompute less
            sk = indexing_to_component(sc, (), sh)
            for ic in compute_indices(ish):
                ik = indexing_to_component(ic, (), ish)
                k = sk*im + ik # Compute the output component index (not used!)

                sops = []
                for j,op in enumerate(ops):
                    # Find the operand component index
                    jk = sk*opims[j] + sum(a*b for a,b in izip(ic, istrides[j]))
                    if jk >= len(op):
                        print
                        print 'DEBUGGING VALUES IN element_wise2:'
                        print o
                        print sh
                        print ish
                        print im, m, tm
                        print j, opims, istrides
                        print
                    sops.append(op[jk])

                res.append(scalar_operator(sops))

        uflacs_assert(tm == len(res), "Size mismatch.")
        return res

    def division(self, o, ops):
        uflacs_assert(len(ops) == 2, "Expecting two operands.")
        def _div(args):
            a, b = args
            return a / b
        return self.element_wise(_div, o, ops)

    def sum(self, o, ops):
        return self.element_wise(sum, o, ops)

    def product(self, o, ops):
        return self.element_wise2(product, o, ops)

    def index_sum(self, o, ops):
        summand, mi = o.operands()
        ii = mi[0]
        fi = summand.free_indices()
        idims = summand.index_dimensions()
        d = idims[ii]

        # Compute "macro-dimensions" before and after i in the total shape of a
        predim = product(summand.shape())
        postdim = 1
        ic = ii.count()
        for jj in fi:
            jc = jj.count()
            if jc < ic:
                predim *= idims[jj]
            elif jc > ic:
                postdim *= idims[jj]

        # Map each flattened total component of summand to
        # flattened total component of indexsum o by removing
        # axis corresponding to summation index ii.
        ss = ops[0] # Scalar subexpressions of summand
        uflacs_assert(len(ss) == predim*postdim*d, "Mismatching number of subexpressions.")
        sops = []
        for i in xrange(predim):
            iind = i*(postdim*d)
            for k in xrange(postdim):
                ind = iind + k
                sops.append([ss[ind + j*postdim] for j in xrange(d)])

        # For each scalar output component, sum over collected subcomponents
        return [sum(sop) for sop in sops]

    # TODO: To implement compound tensor operators such as dot and inner,
    # we need to identify which index to do the contractions over,
    # and build expressions such as sum(a*b for a,b in zip(aops, bops))

def rebuild_scalar_e2i(G, DEBUG=False):
    """Build a new expression2index mapping where each subexpression is scalar valued.

    Input:
    - G.e2i
    - G.V
    - G.V_symbols
    - G.total_unique_symbols

    Output:
    - ne2i - Mapping from scalar subexpressions to a contiguous unique index
    - NV   - Array with reverse mapping from index to expression
    - nvs  - Tuple of ne2i indices corresponding to the last vertex of G.V
    - W    - Array with reconstructed scalar subexpressions for each original symbol
    - terminals - Set of modified terminal expressions (terminals possibly wrapped
                  in grad, restriction and indexed)
    """

    # Data structures
    ne2i = {}
    NV = object_array(G.total_unique_symbols)
    W = object_array(G.total_unique_symbols)
    terminals = set()

    # These types are not expected to be part of the graph at this point
    unexpected = (MultiIndex, Data, UtilityType, Label, ComponentTensor, ListTensor, Transposed, Variable)

    def emit_expression(s, u):
        # Allocate count for scalar expression and
        # store in all cross referenced data structures
        j = ne2i.get(u)
        if j is None:
            j = len(ne2i)
            ne2i[u] = j
            NV[j] = u
        W[s] = u
        emit_expression.W_len = max(emit_expression.W_len, s+1)
        if DEBUG: print 'emitted s, j, u:', s, j, u
    emit_expression.W_len = 0

    reconstruct_scalar_subexpressions = ReconstructScalarSubexpressions()

    handled_symbols = int_array(G.total_unique_symbols)
    for i,v in enumerate(G.V):
        # Find symbols of v components
        vs = G.V_symbols[i]

        if DEBUG: print '\n\n:: i, v, vs ::', i, v, vs

        # Skip if there's nothing new here (should be the case for indexing types)
        if all(handled_symbols[s] for s in vs):
            continue

        #if all(W[s] is not None for s in vs):
        #    continue

        if isinstance(v, unexpected):
            error("Not expecting a %s here!" % type(v))

        for s in vs:
            handled_symbols[s] = 1

        if is_modified_terminal(v):
            if 0: print "Adding terminal: ", repr(v)
            sh = v.shape()
            uflacs_assert(v.free_indices() == (), "Expecting no free indices.")
            if sh == ():
                # Store single modified terminal expression component
                uflacs_assert(len(vs) == 1, "Expecting single symbol for scalar valued modified terminal.")
                s, u = vs[0], v
                emit_expression(s, u)
                terminals.add(v)
            else:
                # Store each terminal expression component
                for s, c in izip(vs, compute_indices(sh)):
                    u = v[c]
                    emit_expression(s, u)
                    # FIXME: Keep modified terminal expression components in the graph that is input here!
                    terminals.add(u)
        else:
            # Find symbols of operands
            sops = []
            for j, vop in enumerate(v.operands()):
                if isinstance(vop, MultiIndex):
                    if not isinstance(v, IndexSum):
                        error("FIXME: Not expecting a %s." % type(v))
                    so = ()
                else:
                    so = G.V_symbols[G.e2i[vop]]
                sops.append(so)

            # Fetch reconstructed operand expressions
            wops = [tuple(W[k] for k in so) for so in sops]

            # Reconstruct scalar subexpressions of v
            w = reconstruct_scalar_subexpressions(v, wops)

            # Store all scalar subexpressions for v symbols
            if not len(vs) == len(w):
                print
                print type(v)
                print v.shape()
                print v.free_indices()
                print len(vs)
                print len(w)
                print
                uflacs_assert(len(vs) == len(w), "Expecting one symbol for each expression.")
            for s,u in izip(vs,w):
                emit_expression(s, u)

    # Reduce size of NV to the actually used parts
    uflacs_assert(all(x is None for x in NV[len(ne2i):]),
                  "Expecting last part of NV to be empty.")
    NV = NV[:len(ne2i)]

    # Find symbols of final v
    vs = G.V_symbols[G.nv-1]
    uflacs_assert(all(handled_symbols[s] for s in vs),
                  "Expecting that all symbols in vs are handled at this point.")
    nvs = [ne2i[W[s]] for s in vs]

    return ne2i, NV, W, terminals, nvs # FIXME: Which returnees do we need?

def rebuild_expression_from_graph(G, DEBUG=False):
    "This is currently only used by tests."
    ne2i, NV, W, terminals, nvs = rebuild_scalar_e2i(G, DEBUG=DEBUG)
    # Find expressions of final v
    w = [NV[k] for k in nvs]
    if len(w) == 1:
        return w[0]
    else:
        return as_vector(w) # TODO: Consider shape of initial v
