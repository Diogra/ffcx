
import ufl
from ufl.classes import (Terminal, GeometricQuantity, ConstantValue,
                         Argument, Coefficient,
                         Grad, Restricted, Indexed,
                         MathFunction)

from uflacs.utils.log import error, uflacs_assert
from uflacs.analysis.datastructures import (int_array, object_array,
                                              CRS, rows_to_crs, rows_dict_to_crs)

def compute_dependencies(e2i, V, ignore_terminal_modifiers=True):
    if ignore_terminal_modifiers:
        terminalish = (Terminal, Grad, Restricted, Indexed)
    else:
        terminalish = (Terminal,)

    n = len(V)
    dependencies = object_array(n)
    k = 0
    for i,v in enumerate(V):
        if isinstance(v, terminalish):
            dependencies[i] = ()
        elif 1:
            dependencies[i] = [e2i[o] for o in v.operands()]
            k += len(dependencies[i])
        else: # Debugging code:
            deps = []
            for o in v.operands():
                j = e2i.get(o)
                if j is None:
                    pass
                else:
                    deps.append(j)

            if any(d is None for d in deps):
                print "DEBUGGING in compute_dependencies:"
                print
                print '\n'.join(map(str,v.operands()))
                print
                print '\n'.join(map(str,deps))
                print

            dependencies[i] = [e2i[o] for o in v.operands()]
            k += len(dependencies[i])

    return rows_to_crs(dependencies, n, k, int)


# TODO: This is probably useless, and not tested:
def find_duplications(V, dependencies, active):
    """Assuming no terminal duplications, find duplications among operators."""
    visited = {}
    dup = {}
    for i,v in enumerate(V):
        if active[i]:
            key = (type(v), tuple(dependencies[i]))
            orig = visited.get(key)
            if orig is None:
                visited[key] = i
            else:
                dup[i] = orig
                active[i] = 0


def mark_active(max_symbol, dependencies, initially_active):
    """FIXME: Cover this with tests.

    Input:
    - max_symbol       - The number of symbols, assumed contiguous in [0,max_symbols).
    - dependencies     - CRS of ints, a mapping from symbol to symbols of arguments.
    - initially_active - Sequence of symbols to be marked as used initially.

    Output:
    - active   - Truth value for each symbol.
    - num_used - Number of true values in active array.
    """

    # Initial state where nothing is marked as used
    active = int_array(max_symbol)
    num_used = 0

    # Seed with initially used symbols
    for s in initially_active:
        active[s] = 1
        num_used += 1

    # Mark dependencies by looping backwards through symbols array
    for s in xrange(max_symbol-1,-1,-1):
        if active[s]:
            for r in dependencies[s]:
                if not active[r]:
                    active[r] = 1
                    num_used += 1

    # Return array marking which symbols are used and the number of positives
    return active, num_used

def default_partition_seed(expr, rank):
    """
    Partition 0: Piecewise constant on each cell (including Real and DG0 coefficients)
    Partition 1: Depends on x
    Partition 2: Depends on x and coefficients
    Partitions [3,3+rank): depend on argument with count partition-3
    """
    # TODO: Use named constants for the partition numbers here

    modifiers = (Grad, Restricted, Indexed) # FIXME: Add CellAvg, FacetAvg types here, others?
    if isinstance(expr, modifiers):
        return default_partition_seed(expr.operands()[0], rank)

    elif isinstance(expr, Argument):
        ac = expr.number()
        assert 0 <= ac < rank
        poffset = 3
        p = poffset + ac
        return p

    elif isinstance(expr, Coefficient):
        if expr.is_cellwise_constant(): # This is crap, doesn't include grad modifier
            return 0
        else:
            return 2

    elif isinstance(expr, GeometricQuantity):
        if expr.is_cellwise_constant(): # This is crap, doesn't include grad modifier
            return 0
        else:
            return 1

    elif isinstance(expr, ConstantValue):
        return 0

    else:
        error("Don't know how to handle %s" % expr)

def mark_partitions(V, active, dependencies, rank,
                    partition_seed=default_partition_seed,
                    partition_combiner=max):
    """FIXME: Cover this with tests.

    Input:
    - V            - Array of expressions.
    - active       - Boolish array.
    - dependencies - CRS with V dependencies.
    - partition_seed - Policy for determining the partition of a terminalish.
    - partition_combiner - Policy for determinging the partition of an operator.

    Output:
    - partitions   - Array of partition int ids.
    """
    n = len(V)
    assert len(active) == n
    assert len(dependencies) == n
    partitions = int_array(n)
    for i, v in enumerate(V):
        deps = dependencies[i]
        if active[i]:
            if len(deps):
                p = partition_combiner([partitions[d] for d in deps])
            else:
                p = partition_seed(v, rank)
        else:
            p = -1
        partitions[i] = p
    return partitions

def build_factorized_partitions():
    num_points = [3]

    #dofrange = (begin, end)
    #dofblock = ()  |  (dofrange0,)  |  (dofrange0, dofrange1)

    partitions = {}

    # partitions["piecewise"] = partition of expressions independent of quadrature and argument loops
    partitions["piecewise"] = []

    # partitions["varying"][np] = partition of expressions dependent on np quadrature but independent of argument loops
    partitions["varying"] = dict((np, []) for np in num_points)

    # partitions["argument"][np][iarg][dofrange] = partition depending on this dofrange of argument iarg
    partitions["argument"] = dict((np, [dict() for i in range(rank)]) for np in num_points)

    # partitions["integrand"][np][dofrange] = partition depending on this dofrange of argument iarg
    partitions["integrand"] = dict((np, dict()) for np in num_points)

def compute_dependency_count(dependencies):
    """FIXME: Test"""
    n = len(dependencies)
    depcount = int_array(n)
    for i in xrange(n):
        for d in dependencies[i]:
            depcount[d] += 1
    return depcount

def invert_dependencies(dependencies, depcount):
    """FIXME: Test"""
    n = len(dependencies)
    m = sum(depcount)
    invdeps = [()]*n
    for i in xrange(n):
        for d in dependencies[i]:
            invdeps[d] = invdeps[d] + (i,)
    return rows_to_crs(invdeps, n, m, int)

def default_cache_score_policy(vtype, ndeps, ninvdeps, partition):
    # Start at 1 and then multiply with various heuristic factors
    s = 1

    # Is the type particularly expensive to compute?
    expensive = (MathFunction,)
    if vtype in expensive: # Could make a type-to-cost mapping, but this should do.
        s *= 20

    # More deps roughly correlates to more operations
    s *= ndeps

    # If it is reused several times let that count significantly
    s *= ninvdeps**3 # 1->1, 2->8, 3->27

    # Finally let partition count for something?
    # Or perhaps we need some more information, such as
    # when x from outer loop is used by y within inner loop.

    return s

def compute_cache_scores(V, active, dependencies, inverse_dependencies, partitions,
                         cache_score_policy=default_cache_score_policy):
    """FIXME: Cover with tests.

    TODO: Experiment with heuristics later when we have functional code generation.
    """
    n = len(V)
    score = int_array(n)
    for i,v in enumerate(V):
        if active[i]:
            deps = dependencies[i]
            ndeps = len(deps)
            invdeps = inverse_dependencies[i]
            ninvdeps = len(invdeps)
            p = partitions[i]
            s = cache_score_policy(type(v), ndeps, ninvdeps, p)
        else:
            s = -1
        score[i] = s
    return score

import heapq
def allocate_registers(active, partitions, targets,
                       scores, max_registers, score_threshold):
    """FIXME: Cover with tests.

    TODO: Allow reuse of registers, reducing memory usage.

    TODO: Probably want to sort within partitions.
    """
    # Check for consistent number of variables
    n = len(scores)
    assert n == len(active)
    assert n == len(partitions)
    num_targets = len(targets)

    # Analyse scores
    min_score = min(scores)
    max_score = max(scores)
    mean_score = sum(scores) // n

    # Can allocate a number of registers up to given threshold
    num_to_allocate = max(num_targets,
                          min(max_registers, n)-num_targets)
    to_allocate = set()

    # For now, just using an arbitrary heuristic algorithm to select m largest scores
    queue = [(-scores[i], i) for i in xrange(n) if active[i]]
    heapq.heapify(queue)

    # Always allocate registers for all targets, for simplicity
    # in the rest of the code generation pipeline
    to_allocate.update(targets)

    # Allocate one register each for max_registers largest symbols
    for r in xrange(num_to_allocate):
        s, i = heapq.heappop(queue)
        if -s <= score_threshold:
            break
        if i in targets:
            continue
        to_allocate.add(i)

    registers_used = len(to_allocate)

    # Some consistency checks
    assert num_to_allocate <= max_registers
    assert registers_used <= num_to_allocate+len(targets)
    assert registers_used <= max(max_registers, len(targets))

    # Mark allocations
    allocations = int_array(n)
    allocations[:] = -1
    for r, i in enumerate(sorted(to_allocate)):
        allocations[i] = r

    # Possible data structures for improved register allocations
    #register_status = int_array(max_registers)

    # Stack/set of free registers (should wrap in stack abstraction):
    #free_registers = int_array(max_registers)
    #num_free_registers = max_registers
    #free_registers[:] = reversed(xrange(max_registers))

    return allocations
