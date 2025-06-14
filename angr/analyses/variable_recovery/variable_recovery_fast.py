# pylint:disable=wrong-import-position,wrong-import-order
from __future__ import annotations
from typing import TYPE_CHECKING
import logging
from collections import defaultdict

import networkx

import claripy
import pyvex
import angr.ailment as ailment
from angr.ailment.expression import VirtualVariable

import angr.errors
from angr import SIM_TYPE_COLLECTIONS
from angr.analyses import AnalysesHub
from angr.storage.memory_mixins.paged_memory.pages.multi_values import MultiValues
from angr.block import Block
from angr.errors import AngrVariableRecoveryError, SimEngineError
from angr.knowledge_plugins import Function
from angr.knowledge_plugins.key_definitions import atoms
from angr.sim_variable import SimStackVariable, SimRegisterVariable, SimVariable, SimMemoryVariable
from angr.engines.vex.claripy.irop import vexop_to_simop
from angr.analyses import ForwardAnalysis, visitors
from angr.analyses.typehoon.typevars import Equivalence, TypeVariable, TypeVariables, Subtype, DerivedTypeVariable
from angr.analyses.typehoon.typeconsts import Int, TypeConstant, BottomType, TopType
from angr.analyses.typehoon.lifter import TypeLifter
from .variable_recovery_base import VariableRecoveryBase, VariableRecoveryStateBase
from .engine_vex import SimEngineVRVEX
from .engine_ail import SimEngineVRAIL
import contextlib


if TYPE_CHECKING:
    from angr.analyses.typehoon.typevars import TypeConstraint

l = logging.getLogger(name=__name__)


class VariableRecoveryFastState(VariableRecoveryStateBase):
    """
    The abstract state of variable recovery analysis.

    :ivar KeyedRegion stack_region: The stack store.
    :ivar KeyedRegion register_region:  The register store.
    """

    def __init__(
        self,
        block_addr,
        analysis,
        arch,
        func,
        project,
        stack_region=None,
        register_region=None,
        global_region=None,
        typevars=None,
        type_constraints=None,
        func_typevar=None,
        delayed_type_constraints=None,
        stack_offset_typevars=None,
        ret_val_size=None,
    ):
        super().__init__(
            block_addr,
            analysis,
            arch,
            func,
            project,
            stack_region=stack_region,
            register_region=register_region,
            global_region=global_region,
            typevars=typevars,
            type_constraints=type_constraints,
            func_typevar=func_typevar,
            delayed_type_constraints=delayed_type_constraints,
            stack_offset_typevars=stack_offset_typevars,
        )
        self.ret_val_size = ret_val_size

    def __repr__(self):
        return f"<VRAbstractState@{self.block_addr:#x}"

    def __eq__(self, other):
        if type(other) is not VariableRecoveryFastState:
            return False
        return self.stack_region == other.stack_region and self.register_region == other.register_region

    def copy(self):
        return VariableRecoveryFastState(
            self.block_addr,
            self._analysis,
            self.arch,
            self.function,
            stack_region=self.stack_region.copy(),
            register_region=self.register_region.copy(),
            global_region=self.global_region.copy(),
            typevars=self.typevars,
            type_constraints=self.type_constraints,
            func_typevar=self.func_typevar,
            delayed_type_constraints=self.delayed_type_constraints,
            stack_offset_typevars=self.stack_offset_typevars,
            project=self.project,
            ret_val_size=self.ret_val_size,
        )

    def merge(
        self, others: tuple[VariableRecoveryFastState, ...], successor=None
    ) -> tuple[VariableRecoveryFastState, bool]:
        """
        Merge two abstract states.

        For any node A whose dominance frontier that the current node (at the current program location) belongs to, we
        create a phi variable V' for each variable V that is defined in A, and then replace all existence of V with V'
        in the merged abstract state.

        :param others: Other abstract states to merge.
        :return:       The merged abstract state.
        """

        self.phi_variables = {}  # A mapping from original variable and its corresponding phi variable
        self.successor_block_addr = successor

        merged_stack_region = self.stack_region.copy()
        merged_stack_region.set_state(self)
        merge_occurred = merged_stack_region.merge([other.stack_region for other in others], None)

        merged_register_region = self.register_region.copy()
        merged_register_region.set_state(self)
        merge_occurred |= merged_register_region.merge([other.register_region for other in others], None)

        merged_global_region = self.global_region.copy()
        merged_global_region.set_state(self)
        merge_occurred |= merged_global_region.merge([other.global_region for other in others], None)

        typevars = self.typevars
        type_constraints = self.type_constraints
        delayed_typeconstraints = self.delayed_type_constraints

        # add subtype constraints for all replacements
        for v0, v1 in self.phi_variables.items():
            # v0 will be replaced by v1
            if not typevars.has_type_variable_for(v1):
                typevars.add_type_variable(v1, TypeVariable())
            if not typevars.has_type_variable_for(v0):
                typevars.add_type_variable(v0, TypeVariable())
            # Assuming v2 = phi(v0, v1), then we know that v0_typevar == v1_typevar == v2_typevar
            # However, it's possible that neither v0 nor v1 will ever be used in future blocks, which not only makes
            # this phi function useless, but also leads to the incorrect assumption that v1_typevar == v2_typevar.
            # Hence, we delay the addition of the equivalence relationship into the type constraints. It is only added
            # when v1 (the new variable that will end up in the state) is ever used in the future.

            # create an equivalence relationship
            equivalence = Equivalence(typevars.get_type_variable(v1), typevars.get_type_variable(v0))
            delayed_typeconstraints[v1].add(equivalence)

        stack_offset_typevars = {}
        all_stack_addr_typevar_offsets = set(self.stack_offset_typevars)
        for other in others:
            all_stack_addr_typevar_offsets.update(other.stack_offset_typevars)
        for offset in all_stack_addr_typevar_offsets:
            all_typevars = set()
            for state in (self, *others):
                typevar = state.stack_offset_typevars.get(offset, None)
                if typevar is not None:
                    all_typevars.add(typevar)

            if len(all_typevars) == 1:
                typevar = all_typevars.pop()
            else:
                typevar = TypeVariable()
                for orig_typevar in all_typevars:
                    type_constraints[self.func_typevar].add(Equivalence(orig_typevar, typevar))
            stack_offset_typevars[offset] = typevar

        ret_val_size = self.ret_val_size
        for o in others:
            if o.ret_val_size is not None and (ret_val_size is None or o.ret_val_size > ret_val_size):
                ret_val_size = o.ret_val_size
                merge_occurred = True

        # clean up
        self.phi_variables = {}
        self.successor_block_addr = None

        state = VariableRecoveryFastState(
            successor,
            self._analysis,
            self.arch,
            self.function,
            stack_region=merged_stack_region,
            register_region=merged_register_region,
            global_region=merged_global_region,
            typevars=typevars,
            type_constraints=type_constraints,
            func_typevar=self.func_typevar,
            delayed_type_constraints=delayed_typeconstraints,
            stack_offset_typevars=stack_offset_typevars,
            project=self.project,
            ret_val_size=ret_val_size,
        )

        return state, merge_occurred

    def downsize(self) -> None:
        pass

    #
    # Util methods
    #

    def _normalize_register_offset(self, offset):  # pylint:disable=no-self-use
        # TODO:

        return offset

    def _to_signed(self, n):
        if n >= 2 ** (self.arch.bits - 1):
            # convert it to a negative number
            return n - 2**self.arch.bits

        return n


class VariableRecoveryFast(ForwardAnalysis, VariableRecoveryBase):  # pylint:disable=abstract-method
    """
    Recover "variables" from a function by keeping track of stack pointer offsets and pattern matching VEX statements.

    If calling conventions are recovered prior to running VariableRecoveryFast, variables can be recognized more
    accurately. However, it is not a requirement. In this case, the function graph you pass must contain information
    indicating the call-out sites inside the analyzed function. These graph edges must be annotated with either
    ``"type": "call"`` or ``"outside": True``.
    """

    def __init__(
        self,
        func: Function | str | int,
        func_graph: networkx.DiGraph | None = None,
        entry_node_addr: int | tuple[int, int | None] | None = None,
        max_iterations: int = 2,
        low_priority=False,
        track_sp=True,
        func_args: list[SimVariable] | None = None,
        store_live_variables=False,
        unify_variables=True,
        func_arg_vvars: dict[int, tuple[VirtualVariable, SimVariable]] | None = None,
        vvar_to_vvar: dict[int, int] | None = None,
        type_hints: list[tuple[atoms.VirtualVariable | atoms.MemoryLocation, str]] | None = None,
    ):
        if not isinstance(func, Function):
            func = self.kb.functions[func]
        func_graph_with_calls = func_graph or func.transition_graph
        call_info = defaultdict(list)
        for node_from, node_to, data in func_graph_with_calls.edges(data=True):
            if data.get("type", None) == "call" or data.get("outside", False):
                with contextlib.suppress(KeyError):
                    call_info[node_from.addr].append(self.kb.functions.get_by_addr(node_to.addr))

        function_graph_visitor = visitors.FunctionGraphVisitor(func, graph=func_graph)

        # Make sure the function is not empty
        if (not func.block_addrs_set or func.startpoint is None) and not func_graph:
            raise AngrVariableRecoveryError(f"Function {func!r} is empty.")

        VariableRecoveryBase.__init__(
            self,
            func,
            max_iterations,
            store_live_variables,
            vvar_to_vvar=vvar_to_vvar,
            func_graph=func_graph_with_calls,
            entry_node_addr=entry_node_addr,
        )
        ForwardAnalysis.__init__(
            self, order_jobs=True, allow_merging=True, allow_widening=False, graph_visitor=function_graph_visitor
        )

        self._low_priority = low_priority
        self._job_ctr = 0
        self._track_sp = track_sp and self.project.arch.sp_offset is not None
        self._func_args = func_args
        self._func_arg_vvars = func_arg_vvars
        self._unify_variables = unify_variables

        # handle type hints
        self.vvar_type_hints = {}
        if type_hints:
            self._parse_type_hints(type_hints)

        self._ail_engine: SimEngineVRAIL = SimEngineVRAIL(
            self.project,
            self.kb,
            call_info=call_info,
            vvar_to_vvar=self.vvar_to_vvar,
            vvar_type_hints=self.vvar_type_hints,
        )
        self._vex_engine: SimEngineVRVEX = SimEngineVRVEX(self.project, self.kb, call_info=call_info)

        self._node_iterations = defaultdict(int)

        self._node_to_cc = {}
        self.var_to_typevars: defaultdict[SimVariable, set[TypeVariable]] = defaultdict(set)
        self.typevars = None
        self.type_constraints: dict[TypeVariable, set[TypeConstraint]] | None = None
        self.func_typevar = TypeVariable(name=func.name)
        self.delayed_type_constraints = None
        self.ret_val_size = None
        self.stack_offset_typevars: dict[int, TypeVariable] = {}

        self._analyze()

        # cleanup (for cpython pickle)
        self.downsize()
        del self._ail_engine
        del self._vex_engine

    #
    # Main analysis routines
    #

    def _pre_analysis(self):
        self.typevars = TypeVariables()
        self.type_constraints = defaultdict(set)
        self.delayed_type_constraints = defaultdict(set)

        self.initialize_dominance_frontiers()

        if self._track_sp:
            # initialize node_to_cc map
            function_nodes = [n for n in self.function.transition_graph.nodes() if isinstance(n, Function)]
            # all nodes that end with a call must be in the _node_to_cc dict
            for func_node in function_nodes:
                for callsite_node in self.function.transition_graph.predecessors(func_node):
                    if func_node.calling_convention is None:
                        # l.warning("Unknown calling convention for %r.", func_node)
                        self._node_to_cc[callsite_node.addr] = None
                    else:
                        self._node_to_cc[callsite_node.addr] = func_node.calling_convention

    def _pre_job_handling(self, job):
        self._job_ctr += 1
        if self._low_priority:
            self._release_gil(self._job_ctr, 100, 0.000001)

    def _initial_abstract_state(self, node):
        state = VariableRecoveryFastState(
            node.addr,
            self,
            self.project.arch,
            self.function,
            project=self.project,
            typevars=self.typevars,
            type_constraints=self.type_constraints,
            func_typevar=self.func_typevar,
            delayed_type_constraints=self.delayed_type_constraints,
            stack_offset_typevars=self.stack_offset_typevars,
        )
        initial_sp = state.stack_address(self.project.arch.bytes if self.project.arch.call_pushes_ret else 0)
        if self.project.arch.sp_offset is not None:
            state.register_region.store(self.project.arch.sp_offset, initial_sp)
        # give it enough stack space
        if self.project.arch.bp_offset is not None:
            state.register_region.store(self.project.arch.bp_offset, initial_sp + 0x100000)

        internal_manager = self.variable_manager[self.function.addr]

        # put a return address on the stack if necessary
        if self.project.arch.call_pushes_ret:
            ret_addr_offset = self.project.arch.bytes
            # find existing variable
            ret_addr_var = next(
                iter(
                    v
                    for v in internal_manager.find_variables_by_stack_offset(ret_addr_offset)
                    if v.category == "return_address"
                ),
                None,
            )
            if ret_addr_var is None:
                ret_addr_var = SimStackVariable(
                    ret_addr_offset,
                    self.project.arch.bytes,
                    base="bp",
                    name="ret_addr",
                    region=self.function.addr,
                    category="return_address",
                    ident=internal_manager.next_variable_ident("stack"),
                )
            ret_addr = claripy.BVS("ret_addr", self.project.arch.bits)
            ret_addr = state.annotate_with_variables(ret_addr, [(0, ret_addr_var)])
            state.stack_region.store(
                state.stack_addr_from_offset(ret_addr_offset), ret_addr, endness=self.project.arch.memory_endness
            )
            internal_manager.add_variable("stack", ret_addr_offset, ret_addr_var)

        if self.project.arch.name.startswith("MIPS"):
            t9_offset, t9_size = self.project.arch.registers["t9"]
            try:
                t9_val = state.register_region.load(t9_offset, t9_size)
                if state.is_top(t9_val):
                    state.register_region.store(t9_offset, claripy.BVV(node.addr, t9_size * 8))
            except angr.errors.SimMemoryMissingError:
                state.register_region.store(t9_offset, claripy.BVV(node.addr, t9_size * 8))

        if self._func_arg_vvars:
            for arg_vvar, arg in self._func_arg_vvars.values():
                if isinstance(arg, SimRegisterVariable):
                    v = claripy.BVS("reg_arg", arg.bits)
                    v = state.annotate_with_variables(v, [(0, arg)])
                    arg_vvar_id = arg_vvar.varid
                    if self.vvar_to_vvar:
                        arg_vvar_id = self.vvar_to_vvar.get(arg_vvar_id, arg_vvar_id)
                    self._ail_engine.vvar_region[arg_vvar_id] = v
                    internal_manager.add_variable("register", arg.reg, arg)
                elif isinstance(arg, SimStackVariable):
                    v = claripy.BVS("stack_arg", arg.bits)
                    v = state.annotate_with_variables(v, [(0, arg)])
                    arg_vvar_id = arg_vvar.varid
                    if self.vvar_to_vvar:
                        arg_vvar_id = self.vvar_to_vvar.get(arg_vvar_id, arg_vvar_id)
                    self._ail_engine.vvar_region[arg_vvar_id] = v
                    internal_manager.add_variable("stack", arg.offset, arg)
                else:
                    raise TypeError(f"Unsupported function argument type {type(arg)}")
        elif self._func_args:
            for arg in self._func_args:
                if isinstance(arg, SimRegisterVariable):
                    v = claripy.BVS("reg_arg", arg.bits)
                    v = state.annotate_with_variables(v, [(0, arg)])
                    state.register_region.store(arg.reg, v)
                    internal_manager.add_variable("register", arg.reg, arg)
                elif isinstance(arg, SimStackVariable):
                    v = claripy.BVS("stack_arg", arg.bits)
                    v = state.annotate_with_variables(v, [(0, arg)])
                    state.stack_region.store(
                        state.stack_addr_from_offset(arg.offset),
                        v,
                        endness=self.project.arch.memory_endness,
                    )
                    internal_manager.add_variable("stack", arg.offset, arg)
                else:
                    raise TypeError(f"Unsupported function argument type {type(arg)}.")

        return state

    def _merge_states(self, node, *states: VariableRecoveryFastState):
        merged_state, merge_occurred = states[0].merge(states[1:], successor=node.addr)
        return merged_state, not merge_occurred

    def _run_on_node(self, node, state):
        """


        :param angr.Block node:
        :param VariableRecoveryState state:
        :return:
        """

        if type(node) is ailment.Block:
            # AIL mode
            block = node
            block_key = node.addr, node.idx
        else:
            # VEX mode, get the block again
            block = self.project.factory.block(node.addr, node.size, opt_level=1, cross_insn_opt=False)
            block_key = node.addr

        state = state.copy()
        state.block_addr = node.addr
        if isinstance(node, ailment.Block):
            state.block_idx = node.idx

        if self._node_iterations[block_key] >= self._max_iterations:
            l.debug("Skip node %#x as we have iterated %d times on it.", node.addr, self._node_iterations[node.addr])
            return False, state

        self._process_block(state, block)

        self._node_iterations[block_key] += 1

        if state.ret_val_size is not None and (self.ret_val_size is None or self.ret_val_size < state.ret_val_size):
            self.ret_val_size = state.ret_val_size

        state.downsize()
        self._outstates[block_key] = state

        return True, state

    def _intra_analysis(self):
        pass

    def _post_analysis(self):
        VariableRecoveryBase._post_analysis(self)

        self.variable_manager["global"].assign_variable_names(labels=self.kb.labels)
        self.variable_manager[self.function.addr].assign_variable_names()

        if self._store_live_variables:
            for addr, state in self._outstates.items():
                self.variable_manager[self.function.addr].set_live_variables(
                    addr,
                    state.downsize_region(state.register_region),
                    state.downsize_region(state.stack_region),
                )

        if self._unify_variables:
            self.variable_manager[self.function.addr].unify_variables()

        # fill in var_to_typevars
        assert self.typevars is not None
        for var, typevar_set in self.typevars._typevars.items():
            self.var_to_typevars[var] = typevar_set

        # unify type variables for global variables
        assert self.type_constraints is not None
        for var, typevars in self.var_to_typevars.items():
            if len(typevars) > 1 and isinstance(var, SimMemoryVariable) and not isinstance(var, SimStackVariable):
                sorted_typevars = sorted(typevars, key=lambda x: str(x))  # pylint:disable=unnecessary-lambda
                for tv in sorted_typevars[1:]:
                    self.type_constraints[self.func_typevar].add(Equivalence(sorted_typevars[0], tv))

        # remove default constraints with size conflicts
        for func_var in self.type_constraints:
            var_to_subtyping: dict[TypeVariable, list[Subtype]] = defaultdict(list)
            for constraint in self.type_constraints[func_var]:
                if isinstance(constraint, Subtype) and isinstance(constraint.sub_type, TypeVariable):
                    var_to_subtyping[constraint.sub_type].append(constraint)

            for constraints in var_to_subtyping.values():
                if len(constraints) <= 1:
                    continue
                default_subtyping_constraints = set()
                has_nondefault_subtyping_constraints = False
                for constraint in constraints:
                    if isinstance(constraint.super_type, Int):
                        default_subtyping_constraints.add(constraint)
                    elif isinstance(constraint.super_type, DerivedTypeVariable) and constraint.super_type.labels:
                        has_nondefault_subtyping_constraints = True
                if has_nondefault_subtyping_constraints:
                    self.type_constraints[func_var].difference_update(default_subtyping_constraints)

        self.variable_manager[self.function.addr].ret_val_size = self.ret_val_size

        self.delayed_type_constraints = None

    #
    # Private methods
    #

    @staticmethod
    def _get_irconst(value, size):
        mapping = {
            1: pyvex.const.U1,
            8: pyvex.const.U8,
            16: pyvex.const.U16,
            32: pyvex.const.U32,
            64: pyvex.const.U64,
            128: pyvex.const.V128,
            256: pyvex.const.V256,
        }
        if size not in mapping:
            raise TypeError(f"Unsupported size {size}.")
        return mapping.get(size)(value)

    def _peephole_optimize(self, block: Block):
        # find regN = xor(regN, regN) and replace it with PUT(regN) = 0
        i = 0
        while i < len(block.vex.statements) - 3:
            stmt0 = block.vex.statements[i]
            next_i = i + 1
            if isinstance(stmt0, pyvex.IRStmt.WrTmp) and isinstance(stmt0.data, pyvex.IRStmt.Get):
                stmt1 = block.vex.statements[i + 1]
                if isinstance(stmt1, pyvex.IRStmt.WrTmp) and isinstance(stmt1.data, pyvex.IRStmt.Get):
                    next_i = i + 2
                    if stmt0.data.offset == stmt1.data.offset and stmt0.data.ty == stmt1.data.ty:
                        next_i = i + 3
                        reg_offset = stmt0.data.offset
                        tmp0 = stmt0.tmp
                        tmp1 = stmt1.tmp
                        stmt2 = block.vex.statements[i + 2]
                        if (
                            isinstance(stmt2, pyvex.IRStmt.WrTmp)
                            and isinstance(stmt2.data, pyvex.IRExpr.Binop)
                            and isinstance(stmt2.data.args[0], pyvex.IRExpr.RdTmp)
                            and isinstance(stmt2.data.args[1], pyvex.IRExpr.RdTmp)
                            and {stmt2.data.args[0].tmp, stmt2.data.args[1].tmp} == {tmp0, tmp1}
                            and vexop_to_simop(stmt2.data.op)._generic_name == "Xor"
                        ):
                            # found it!
                            # make a copy so we don't trash the cached VEX IRSB
                            block._vex = block.vex.copy()
                            block.vex.statements[i] = pyvex.IRStmt.NoOp()
                            block.vex.statements[i + 1] = pyvex.IRStmt.NoOp()
                            zero = pyvex.IRExpr.Const(self._get_irconst(0, block.vex.tyenv.sizeof(tmp0)))
                            block.vex.statements[i + 2] = pyvex.IRStmt.Put(zero, reg_offset)
            i = next_i
        return block

    def _process_block(self, state, block):  # pylint:disable=no-self-use
        """
        Scan through all statements and perform the following tasks:
        - Find stack pointers and the VEX temporary variable storing stack pointers
        - Selectively calculate VEX statements
        - Track memory loading and mark stack and global variables accordingly

        :param angr.Block block:
        :return:
        """

        l.debug("Processing block %#x.", block.addr)

        if isinstance(block, Block):
            try:
                _ = block.vex
            except SimEngineError:
                # the block does not exist or lifting failed
                return
            block = self._peephole_optimize(block)

        processor = self._ail_engine if isinstance(block, ailment.Block) else self._vex_engine
        processor.process(state, block=block, fail_fast=self._fail_fast)  # type: ignore

        if self._track_sp and block.addr in self._node_to_cc:
            # readjusting sp at the end for blocks that end in a call
            sp: MultiValues = state.register_region.load(self.project.arch.sp_offset, size=self.project.arch.bytes)
            sp_v = sp.one_value()
            if sp_v is None:
                l.warning("Unexpected stack pointer value at the end of the function. Pick the first one.")
                sp_v = next(iter(next(iter(sp.values()))))

            adjusted = False

            # make a guess
            # of course, this will fail miserably if the function called is not cdecl
            if not adjusted and self.project.arch.call_pushes_ret:
                sp_v += self.project.arch.bytes
                adjusted = True

            if adjusted:
                state.register_region.store(self.project.arch.sp_offset, sp_v)

    def _parse_type_hints(self, type_hints: list[tuple[atoms.VirtualVariable | atoms.MemoryLocation, str]]) -> None:
        self.vvar_type_hints = {}
        for loc, type_hint_str in type_hints:
            if isinstance(loc, atoms.VirtualVariable):
                type_hint = self._parse_type_hint(type_hint_str)
                if type_hint is not None:
                    self.vvar_type_hints[loc.varid] = type_hint
            # TODO: Handle other types of locations

    def _parse_type_hint(self, type_hint_str: str) -> TypeConstant | None:
        ty = SIM_TYPE_COLLECTIONS["cpp::std"].get(type_hint_str)
        if ty is None:
            return None
        ty = ty.with_arch(self.project.arch)
        lifted = TypeLifter(self.project.arch.bits).lift(ty)
        return None if isinstance(lifted, (BottomType, TopType)) else lifted


AnalysesHub.register_default("VariableRecoveryFast", VariableRecoveryFast)
