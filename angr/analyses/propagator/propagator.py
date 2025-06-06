# pylint:disable=isinstance-second-argument-not-valid-type
from __future__ import annotations
from typing import Any
import logging
import time
import contextlib

import claripy
import angr.ailment as ailment
import pyvex

from angr.code_location import CodeLocation
from angr.analyses import ForwardAnalysis, visitors
from angr.knowledge_plugins.propagations.propagation_model import PropagationModel
from angr.knowledge_plugins.propagations.states import PropagatorVEXState, PropagatorState
from angr import sim_options
from angr.analyses import register_analysis
from angr.analyses.analysis import Analysis
from .engine_vex import SimEnginePropagatorVEX


_l = logging.getLogger(name=__name__)


class PropagatorAnalysis(ForwardAnalysis, Analysis):  # pylint:disable=abstract-method
    """
    PropagatorAnalysis implements copy propagation. It propagates values (either constant values or variables) and
    expressions inside a block or across a function.

    PropagatorAnalysis only supports VEX. For AIL, please use SPropagator.

    PropagatorAnalysis performs certain arithmetic operations between constants, including but are not limited to:

    - addition
    - subtraction
    - multiplication
    - division
    - xor

    It also performs the following memory operations:

    - Loading values from a known address
    - Writing values to a stack variable
    """

    def __init__(
        self,
        func=None,
        block=None,
        func_graph=None,
        base_state=None,
        max_iterations=30,
        load_callback=None,
        stack_pointer_tracker=None,
        only_consts=False,
        completed_funcs=None,
        do_binops=True,
        store_tops=True,
        vex_cross_insn_opt=False,
        func_addr: int | None = None,
        gp: int | None = None,
        cache_results: bool = False,
        key_prefix: str | None = None,
        profiling: bool = False,
    ):
        if block is None and func is not None:
            # only func is specified. traversing a function
            self.flavor = "function"
        elif block is not None:
            # traversing a block (but func might be specified at the same time to provide extra information, e.g., the
            # value for register t9 for MIPS32/64 binaries)
            self.flavor = "block"
        else:
            raise ValueError("Unsupported analysis target.")

        start = time.perf_counter_ns() / 1000000 if profiling else 0

        self._base_state = base_state
        self._function = func
        self._func_addr = func_addr if func_addr is not None else (None if func is None else func.addr)
        self._block = block
        self._block_addr = block.addr if block is not None else None
        self._max_iterations = max_iterations
        self._load_callback = load_callback
        self._stack_pointer_tracker = stack_pointer_tracker  # only used when analyzing AIL functions
        self._only_consts = only_consts
        self._completed_funcs = completed_funcs
        self._do_binops = do_binops
        self._store_tops = store_tops
        self._vex_cross_insn_opt = vex_cross_insn_opt
        self._gp = gp
        self._prop_key_prefix = key_prefix
        self._cache_results = cache_results
        self._initial_codeloc: CodeLocation
        self.stmts_to_remove: set[CodeLocation] = set()
        if self.flavor == "function":
            self._initial_codeloc = CodeLocation(self._func_addr, stmt_idx=0, ins_addr=self._func_addr)
        else:  # flavor == "block"
            self._initial_codeloc = CodeLocation(self._block_addr, stmt_idx=0, ins_addr=self._block_addr)

        self.model: PropagationModel = None

        if self._cache_results:
            # Resume the analysis from the previously unfinished result
            self.model = self.kb.propagations.get(self.prop_key, None)

        if self.model is None:
            self.model = PropagationModel(
                self.prop_key,
                function=self._function,
            )
            cache_used = False
        else:
            cache_used = True

        graph_visitor: visitors.SingleNodeGraphVisitor | visitors.FunctionGraphVisitor
        if self.flavor == "block":
            graph_visitor = None
            if self._cache_results:
                graph_visitor: visitors.SingleNodeGraphVisitor | None = self.model.graph_visitor

            if graph_visitor is None:
                graph_visitor = visitors.SingleNodeGraphVisitor(block)

        elif self.flavor == "function":
            graph_visitor = None
            if self._cache_results:
                graph_visitor: visitors.FunctionGraphVisitor | None = self.model.graph_visitor
                if graph_visitor is not None:
                    # resume
                    resumed = graph_visitor.resume_with_new_graph(func_graph if func_graph is not None else func.graph)
                    if not resumed:
                        # clean up...
                        self.model = PropagationModel(self.prop_key, function=self._function)

            if graph_visitor is None:
                graph_visitor = visitors.FunctionGraphVisitor(func, func_graph)
                self.model.graph_visitor = graph_visitor

        else:
            raise TypeError(f"Unsupported flavor {self.flavor}")

        ForwardAnalysis.__init__(
            self, order_jobs=True, allow_merging=True, allow_widening=False, graph_visitor=graph_visitor
        )

        bp_as_gpr = False
        the_func = None
        if self._function is not None:
            the_func = self._function
        else:
            if self._func_addr is not None:
                with contextlib.suppress(KeyError):
                    the_func = self.kb.functions.get_by_addr(self._func_addr)
        if the_func is not None:
            bp_as_gpr = the_func.info.get("bp_as_gpr", False)

        # pyright says pylint is wrong about this
        self._engine_vex = SimEnginePropagatorVEX(  # pylint: disable=abstract-class-instantiated
            project=self.project,
            bp_as_gpr=bp_as_gpr,
        )

        # optimization: skip state copying for the initial state
        self._initial_state = None

        # performance counters
        self._analyzed_states: int = 0

        self._analyze()

        if self._cache_results:
            # update the cache
            self.kb.propagations.update(self.prop_key, self.model)

        if profiling:
            elapsed = time.perf_counter_ns() / 1000000 - start
            if self.flavor == "function":
                _l.warning("%r:", self._function)
                _l.warning("  Blocks: %d", len(self._function.block_addrs_set))
            else:
                _l.warning("%r:", self._block)
            _l.warning("  Time elapsed: %0.02f milliseconds", elapsed)
            _l.warning("  Cache used: %s", cache_used)
            _l.warning("  Analyzed states: %d", self._analyzed_states)

    @property
    def prop_key(self) -> tuple[str | None, str, int, bool, bool, bool]:
        """
        Gets a key that represents the function and the "flavor" of the propagation result.
        """
        addr = self._func_addr if self._func_addr is not None else self._block_addr
        return self._prop_key_prefix, self.flavor, addr, self._do_binops, self._only_consts, self._vex_cross_insn_opt

    @property
    def replacements(self):
        return self.model.replacements

    @replacements.setter
    def replacements(self, v):
        self.model.replacements = v

    #
    # Main analysis routines
    #

    def _node_key(self, node: ailment.Block | pyvex.IRSB) -> Any:
        if type(node) is ailment.Block:
            return node.addr, node.idx
        if type(node) is pyvex.IRSB:
            return node.addr
        # fallback
        return node

    def _pre_analysis(self):
        pass

    def _pre_job_handling(self, job):
        pass

    def _initial_abstract_state(self, node):
        self._initial_state = PropagatorVEXState.initial_state(
            self.project,
            only_consts=self._only_consts,
            gp=self._gp,
            do_binops=self._do_binops,
            store_tops=self._store_tops,
            func_addr=self._func_addr,
            max_prop_expr_occurrence=1 if self.flavor == "function" else 0,
            initial_codeloc=self._initial_codeloc,
            model=self.model,
        )
        return self._initial_state

    def _merge_states(self, node, *states: PropagatorState):
        merged_state, merge_occurred = states[0].merge(*states[1:])
        return merged_state, not merge_occurred

    def _run_on_node(self, node, state):
        self._analyzed_states += 1

        block = self.project.factory.block(node.addr, node.size, opt_level=1, cross_insn_opt=self._vex_cross_insn_opt)
        block_key = node.addr
        engine = self._engine_vex
        if block.size == 0:
            # maybe the block is not decodeable
            return False, state

        if state is not self._initial_state:
            # make a copy of the state if it's not the initial state
            state = state.copy()
            state.init_replacements()
        else:
            # clear self._initial_state so that we *do not* run this optimization again!
            self._initial_state = None

        # Suppress spurious output
        if self._base_state is not None:
            self._base_state.options.add(sim_options.SYMBOL_FILL_UNCONSTRAINED_REGISTERS)
            self._base_state.options.add(sim_options.SYMBOL_FILL_UNCONSTRAINED_MEMORY)

        self.model.input_states[block_key] = state.copy()

        state = engine.process(
            state,
            block=block,
            project=self.project,
            base_state=self._base_state,
            load_callback=self._load_callback,
            fail_fast=self._fail_fast,
        )
        state.filter_replacements()

        self.model.node_iterations[block_key] += 1
        self.model.states[block_key] = state
        self.model.block_initial_reg_values.update(state.block_initial_reg_values)

        if self.model.replacements is None:
            self.model.replacements = state._replacements
        else:
            PropagatorState.merge_replacements(self.model.replacements, state._replacements)

        # TODO: Clear registers according to calling conventions

        if self.model.node_iterations[block_key] < self._max_iterations:
            return None, state
        return False, state

    def _process_input_state_for_successor(self, node, successor, input_state: PropagatorVEXState):
        if self._only_consts and isinstance(input_state, PropagatorVEXState):
            key = node.addr, successor.addr
            if key in self.model.block_initial_reg_values:
                input_state: PropagatorVEXState = input_state.copy()
                for reg_offset, reg_size, value in self.model.block_initial_reg_values[key]:
                    input_state.store_register(reg_offset, reg_size, claripy.BVV(value, reg_size * 8))
        return input_state

    def _intra_analysis(self):
        pass

    def _check_func_complete(self, func):
        """
        Checks if a function is completely created by the CFG. Completed
        functions are passed to the Propagator at initialization. Defaults to
        being empty if no pass is initiated.

        :param func:    Function to check (knowledge_plugins.functions.function.Function)
        :return:        Bool
        """
        complete = False
        if self._completed_funcs is None:
            return complete

        if func.addr in self._completed_funcs:
            complete = True

        return complete

    def _post_analysis(self):
        """
        Post Analysis of Propagation().
        We add the current propagation replacements result to the kb if the
        function has already been completed in cfg creation.
        """

        # Filter replacements and remove all TOP values
        if self.model.replacements is not None:
            for codeloc in list(self.model.replacements.keys()):
                filtered_rep = {}
                for k, v in self.model.replacements[codeloc].items():
                    if isinstance(v, claripy.ast.Base) and not PropagatorState.is_top(v):
                        filtered_rep[k] = v
                self.model.replacements[codeloc] = filtered_rep

        if self._cache_results:
            self.kb.propagations.update(self.prop_key, self.model)

    def _analyze(self):
        """
        The main analysis for Propagator. Overwritten to include an optimization to stop
        analysis if we have already analyzed the entire function once.
        """
        self._pre_analysis()

        # normal analysis execution
        if self._graph_visitor is None:
            # There is no base graph that we can rely on. The analysis itself should generate successors for the
            # current job.
            # An example is the CFG recovery.

            self._analysis_core_baremetal()

        else:
            # We have a base graph to follow. Just handle the current job.

            self._analysis_core_graph()

        self._post_analysis()


register_analysis(PropagatorAnalysis, "Propagator")
