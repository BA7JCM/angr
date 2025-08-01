from __future__ import annotations
from typing import TYPE_CHECKING, cast, Literal
from collections.abc import Iterable, Callable
from dataclasses import dataclass, field
import logging
from functools import wraps
from cle import Symbol
from cle.backends import ELF
import claripy

from angr.storage.memory_mixins.paged_memory.pages.multi_values import MultiValues
from angr.sim_type import SimTypeBottom
from angr.knowledge_plugins.key_definitions.atoms import Atom, Register, MemoryLocation, SpOffset
from angr.knowledge_plugins.key_definitions.tag import Tag
from angr.calling_conventions import SimCC
from angr.sim_type import SimTypeFunction
from angr.knowledge_plugins.key_definitions.definition import Definition
from angr.knowledge_plugins.functions import Function
from angr.code_location import CodeLocation, ExternalCodeLocation
from angr.knowledge_plugins.key_definitions.constants import ObservationPointType
from angr.utils.types import dereference_simtype_by_lib


if TYPE_CHECKING:
    from angr.knowledge_plugins.key_definitions.rd_model import ReachingDefinitionsModel
    from angr.analyses.reaching_definitions.rd_state import ReachingDefinitionsState
    from angr.analyses.reaching_definitions.reaching_definitions import ReachingDefinitionsAnalysis, ObservationPoint

l = logging.getLogger(__name__)


def get_exit_livedefinitions(func: Function, rda_model: ReachingDefinitionsModel):
    """
    Get LiveDefinitions at all exits of a function, merge them, and return.
    """
    lds = []
    for block in func.ret_sites:
        ld = rda_model.get_observation_by_node(block.addr, ObservationPointType.OP_AFTER)
        if ld is None:
            continue
        lds.append(ld)
    if len(lds) == 1:
        return lds[0]
    if len(lds) == 0:
        return None
    return lds[0].merge(*lds[1:])[0]


@dataclass
class FunctionEffect:
    """
    A single effect that a function summary may apply to the state. This is largely an implementation detail; use
    `FunctionCallData.depends` instead.
    """

    dest: Atom | None
    sources: set[Atom]
    value: MultiValues | None = None
    sources_defns: set[Definition] | None = None
    apply_at_callsite: bool = False
    tags: set[Tag] | None = None


@dataclass
class FunctionCallData:
    """
    A bundle of intermediate data used when computing the sum effect of a function during ReachingDefinitionsAnalysis.

    RDA engine contract:

    - Construct one of these before calling `FunctionHandler.handle_function`. Fill it with as many fields as you can
      realistically provide without duplicating effort.
    - Provide `callsite_codeloc` as either the call statement (AIL) or the default exit of the default statement of the
      calling block (VEX)
    - Provide `function_codeloc` as the callee address with `stmt_idx=0``.

    Function handler contract:

    - If redefine_locals is unset, do not adjust any artifacts of the function call abstraction, such as the stack
      pointer, the caller saved registers, etc.
    - If caller_will_handle_single_ret is set, and there is a single entry in `ret_atoms`, do not apply to the state
      effects modifying this atom. Instead, set `ret_values` and `ret_values_deps` to the values and deps which are
      used constructing these values.
    """

    callsite_codeloc: CodeLocation
    function_codeloc: CodeLocation
    address_multi: MultiValues[claripy.ast.BV | claripy.ast.FP] | None
    address: int | None = None
    symbol: Symbol | None = None
    function: Function | None = None
    name: str | None = None
    cc: SimCC | None = None
    prototype: SimTypeFunction | None = None
    args_atoms: list[set[Atom]] | None = None
    args_values: list[MultiValues[claripy.ast.BV | claripy.ast.FP]] | None = None
    ret_atoms: set[Atom] | None = None
    redefine_locals: bool = True
    visited_blocks: set[int] | None = None
    effects: list[FunctionEffect] = field(default_factory=list)
    ret_values: MultiValues[claripy.ast.BV | claripy.ast.FP] | None = None
    ret_values_deps: set[Definition] | None = None
    caller_will_handle_single_ret: bool = False
    guessed_cc: bool = False
    guessed_prototype: bool = False
    retaddr_popped: bool = False

    def has_clobbered(self, dest: Atom) -> bool:
        """
        Determines whether the given atom already has effects applied
        """
        if isinstance(dest, Register):
            for effect in self.effects:
                if not isinstance(effect.dest, Register):
                    continue
                reg = effect.dest
                if dest.reg_offset + dest.size <= reg.reg_offset or dest.reg_offset >= reg.reg_offset + reg.size:
                    # no overlap
                    continue
                return True
            return False
        if isinstance(dest, MemoryLocation) and isinstance(dest.addr, SpOffset):
            for effect in self.effects:
                stkarg = effect.dest
                if not isinstance(stkarg, MemoryLocation) or not isinstance(stkarg.addr, SpOffset):
                    continue
                if (
                    dest.addr.offset + dest.size <= stkarg.addr.offset
                    or stkarg.addr.offset + stkarg.size <= dest.addr.offset
                ):
                    # no overlap
                    continue
                return True
            return False
        # unsupported
        return False

    def depends(
        self,
        dest: Atom | Iterable[Atom] | None,
        *sources: Atom | Iterable[Atom],
        value: MultiValues | claripy.ast.BV | bytes | int | None = None,
        apply_at_callsite: bool = False,
        tags: set[Tag] | None = None,
    ):
        """
        Mark a single effect of the current function, including the atom being modified, the input atoms on which that
        output atom depends, the precise (or imprecise!) value to store, and whether the effect should be applied
        during the function or afterwards, at the callsite.

        The tags are used to annotate the Definition of the Atom that will be created,
        when the function effects are applied to the state.

        The atom being modified may be None to mark uses of the source atoms which do not have any explicit sinks.
        """
        if dest is None and value is not None:
            raise TypeError("Cannot provide value without a destination to write it to")

        if dest is not None and not isinstance(dest, Atom):
            for dest2 in dest:
                self.depends(dest2, *sources, value=value, apply_at_callsite=apply_at_callsite, tags=tags)
            return

        if isinstance(value, int):
            assert dest is not None
            value = claripy.BVV(value, dest.size * 8)
        elif isinstance(value, bytes):
            value = claripy.BVV(value)
        if isinstance(value, claripy.ast.BV):
            value = MultiValues(value)
        assert value is None or isinstance(value, MultiValues)
        if dest is not None and self.has_clobbered(dest):
            l.warning(
                "Function handler for %s seems to be implemented incorrectly - "
                "you're supposed to call depends() exactly once per dependant atom",
                self.address,
            )
        else:
            self.effects.append(
                FunctionEffect(
                    dest,
                    set().union(*({src} if isinstance(src, Atom) else set(src) for src in sources)),
                    value=value,
                    apply_at_callsite=apply_at_callsite,
                    tags=tags,
                )
            )

    def reset_prototype(
        self, prototype: SimTypeFunction, state: ReachingDefinitionsState, soft_reset: bool = False
    ) -> set[Atom]:
        self.prototype = prototype.with_arch(state.arch)
        if not soft_reset:
            self.args_atoms = self.args_values = self.ret_atoms = None

        args_atoms_from_values = set()
        if self.args_atoms is None and self.args_values is not None:
            self.args_atoms = [
                set().union(
                    *({defn.atom for defn in state.extract_defs(value)} for values in mv.values() for value in values)
                )
                for mv in self.args_values
            ]
            for atoms_set in self.args_atoms:
                args_atoms_from_values |= atoms_set
        elif self.args_atoms is None and self.cc is not None and self.prototype is not None:
            self.args_atoms = FunctionHandler.c_args_as_atoms(state, self.cc, self.prototype)
        if (
            self.ret_atoms is None
            and self.cc is not None
            and self.prototype is not None
            and self.prototype.returnty is not None
        ):
            self.ret_atoms = FunctionHandler.c_return_as_atoms(state, self.cc, self.prototype)
        return args_atoms_from_values


class FunctionCallDataUnwrapped(FunctionCallData):
    """
    A subclass of FunctionCallData which asserts that many of its members are non-None at construction time.
    Typechecks be gone!
    """

    address_multi: MultiValues  # type: ignore[reportIncompatibleVariableOverride]
    address: int  # type: ignore[reportIncompatibleVariableOverride]
    symbol: Symbol
    function: Function  # type: ignore[reportIncompatibleVariableOverride]
    name: str  # type: ignore[reportIncompatibleVariableOverride]
    cc: SimCC  # type: ignore[reportIncompatibleVariableOverride]
    prototype: SimTypeFunction  # type: ignore[reportIncompatibleVariableOverride]
    args_atoms: list[set[Atom]]  # type: ignore[reportIncompatibleVariableOverride]
    args_values: list[MultiValues]  # type: ignore[reportIncompatibleVariableOverride]
    ret_atoms: set[Atom]  # type: ignore[reportIncompatibleVariableOverride]

    def __init__(self, inner: FunctionCallData):
        d = dict(inner.__dict__)
        annotations = type(self).__annotations__  # pylint: disable=no-member
        for k, v in d.items():
            assert (
                v is not None or k not in annotations
            ), f"Failed to unwrap field {k} - this function is more complicated than you're ready for!"
            assert v is not None, "Members of FunctionCallDataUnwrapped may not be None"
        super().__init__(**d)

    @staticmethod
    @wraps
    def decorate(
        f: Callable[[FunctionHandler, ReachingDefinitionsState, FunctionCallDataUnwrapped], None],
    ) -> Callable[[FunctionHandler, ReachingDefinitionsState, FunctionCallData], None]:
        """
        Decorate a function handler method with this to make it take a FunctionCallDataUnwrapped instead of a
        FunctionCallData.
        """

        def inner(self: FunctionHandler, state: ReachingDefinitionsState, data: FunctionCallData):
            f(self, state, FunctionCallDataUnwrapped(data))

        return inner


def _mk_wrapper(func, iself):
    return lambda *args, **kwargs: func(iself, *args, **kwargs)


@dataclass
class FunctionCallRelationships:
    """
    Produced by the function handler, provides associated callsite info and function input/output definitions.
    """

    callsite: CodeLocation
    target: int | None
    args_defns: list[set[Definition]]
    other_input_defns: set[Definition]
    ret_defns: set[Definition]
    other_output_defns: set[Definition]


# pylint: disable=unused-argument, no-self-use
class FunctionHandler:
    """
    A mechanism for summarizing a function call's effect on a program for ReachingDefinitionsAnalysis.
    """

    def __init__(self, interfunction_level: int = 0, extra_impls: Iterable[type[FunctionHandler]] | None = None):
        """
        :param interfunction_level: Maximum depth in to continue local function exploration
        :param extra_impls: FunctionHandler classes to implement beyond what's implemented in function_handler_library
        """

        self.interfunction_level: int = interfunction_level

        if extra_impls is None:
            return

        for extra_handler in extra_impls:
            for cls in extra_handler.__mro__:
                for name, func in vars(cls).items():
                    if name.startswith("handle_impl_"):
                        setattr(self, name, _mk_wrapper(func, self))

    def hook(self, analysis: ReachingDefinitionsAnalysis) -> FunctionHandler:
        """
        Attach this instance of the function handler to an instance of RDA.
        """
        return self

    def make_function_codeloc(
        self, target: None | int | MultiValues, callsite: CodeLocation, callsite_func_addr: int | None
    ):
        """
        The RDA engine will call this function to transform a callsite CodeLocation into a callee CodeLocation.
        """
        if isinstance(target, MultiValues):
            target_bv = target.one_value()
            target_int = target_bv.args[0] if target_bv is not None and target_bv.op == "BVV" else None
        else:
            target_int = target
        if callsite.context is None:
            return CodeLocation(target_int, stmt_idx=None, context=None)
        if type(callsite.context) is tuple and callsite_func_addr is not None:
            return CodeLocation(target_int, stmt_idx=None, context=(callsite.block_addr, *callsite.context))
        raise TypeError("Please implement FunctionHandler.make_function_codeloc for your special context sensitivity")

    def handle_function(self, state: ReachingDefinitionsState, data: FunctionCallData):
        """
        The main entry point for the function handler. Called with a RDA state and a FunctionCallData, it is expected
        to update the state and the data as per the contracts described on FunctionCallData.

        You can override this method to take full control over how data is processed, or override any of the following
        to use the higher-level interface (data.depends()):

        - `handle_impl_<function name>` - used for `<function name>`.
        - `handle_local_function` - used for any function (excluding plt stubs) whose address is inside the main binary.
        - `handle_external_function` - used for any function or plt stub whose address is outside the main binary.
        - `handle_indirect_function` - used for any function whose target cannot be resolved.
        - `handle_generic_function` - used as a default if none of the above are overridden.

        Each of them take the same signature as `handle_function`.
        """
        # META
        assert state.analysis is not None
        assert state.analysis.project.loader.main_object is not None
        if data.address is None and data.address_multi is not None:
            for vs in data.address_multi.values():
                for val in vs:
                    if val is not None and val.op == "BVV":
                        data.address = val.args[0]
                        break
                if data.address is not None:
                    break
        if data.symbol is None and data.address is not None:
            data.symbol = state.analysis.project.loader.find_symbol(data.address)
        if data.function is None and data.address is not None:
            data.function = state.analysis.project.kb.functions.get(data.address, None)
        if data.name is None and data.function is not None:
            data.name = data.function.name
        if data.name is None and data.symbol is not None:
            data.name = data.symbol.name
        if data.cc is None and data.function is not None:
            data.cc = data.function.calling_convention
        if data.prototype is None and data.function is not None:
            data.prototype = data.function.prototype
        hook_libname = None
        if data.address is not None and (data.cc is None or data.prototype is None):
            hook = (
                None
                if not state.analysis.project.is_hooked(data.address)
                else state.analysis.project.hooked_by(data.address)
            )
            if (
                hook is None
                and isinstance(state.analysis.project.loader.main_object, ELF)
                and data.address in state.analysis.project.loader.main_object.reverse_plt
            ):
                plt_name = state.analysis.project.loader.main_object.reverse_plt[data.address]
                if state.analysis.project.loader.find_symbol(plt_name) is not None:
                    hook = state.analysis.project.symbol_hooked_by(plt_name)
            if data.cc is None and hook is not None:
                data.cc = hook.cc
            if data.prototype is None and hook is not None and hook.prototype is not None:
                data.prototype = hook.prototype.with_arch(state.arch)
                data.guessed_prototype = hook.guessed_prototype
                hook_libname = hook.library_name

        # fallback to the default calling convention and prototype
        if data.cc is None:
            data.cc = state.analysis.project.factory.cc()
            data.guessed_cc = True
        if data.prototype is None:
            data.prototype = state.analysis.project.factory.function_prototype()
            data.guessed_prototype = True

        if data.prototype is not None:
            # make sure the function prototype is resolved.
            # TODO: Cache resolved function prototypes globally
            prototype_libname = (
                data.function.prototype_libname
                if data.function is not None and data.function.prototype_libname
                else hook_libname
            )
            if prototype_libname is not None:
                prototype = dereference_simtype_by_lib(data.prototype, prototype_libname)
                data.prototype = cast(SimTypeFunction, prototype)

        if isinstance(data.prototype, SimTypeFunction):
            args_atoms_from_values = data.reset_prototype(data.prototype, state, soft_reset=True)
        else:
            args_atoms_from_values = set()

        # PROCESS
        state.move_codelocs(data.function_codeloc)
        if data.name is not None and hasattr(self, f"handle_impl_{data.name}"):
            handler = getattr(self, f"handle_impl_{data.name}")
        elif data.address is not None:
            if (data.symbol is None and state.analysis.project.loader.main_object.contains_addr(data.address)) or (
                data.symbol is not None and data.symbol.owner is state.analysis.project.loader.main_object
            ):
                handler = self.handle_local_function
            else:
                handler = self.handle_external_function

        else:
            handler = self.handle_indirect_function

        handler(state, data)

        # a call expression does not overwrite or redefine any local registers
        if data.redefine_locals:
            if data.cc is not None:
                for reg in self.caller_saved_regs_as_atoms(state, data.cc):
                    if not data.has_clobbered(reg):
                        data.depends(reg)
            if state.arch.call_pushes_ret and not data.retaddr_popped:
                sp_atom = self.stack_pointer_as_atom(state)
                if not data.has_clobbered(sp_atom):  # let the user override the stack pointer if they want
                    new_sp = None
                    sp_val = state.live_definitions.get_values(sp_atom)
                    if sp_val is not None:
                        one_sp_val = sp_val.one_value()
                        if one_sp_val is not None:
                            # call_sp_fix is the sp movement after the call instruction executes, which means it is
                            # usually a negative number if the stack grows towards a lower address. when we return,
                            # we should subtract this negative number from the current stack pointer to keep the stack
                            # balanced.
                            new_sp = MultiValues(one_sp_val - state.arch.call_sp_fix)
                    data.depends(sp_atom, value=new_sp)

        # OUTPUT
        args_defns = [
            set().union(*(state.get_definitions(atom) for atom in atoms)) for atoms in (data.args_atoms or set())
        ]
        all_args_defns = set().union(*args_defns)
        other_input_defns = set()
        ret_defns = set()
        other_output_defns = set()

        # translate all the dep atoms into dep defns
        for effect in data.effects:
            if effect.sources_defns is None and effect.sources:
                effect.sources_defns = set().union(*(state.get_definitions(atom) for atom in effect.sources))
                if not effect.sources_defns:
                    effect.sources_defns = {Definition(atom, ExternalCodeLocation()) for atom in effect.sources}
                other_input_defns |= effect.sources_defns - all_args_defns
        # apply the effects, with the ones marked with apply_at_callsite=False applied first
        for effect in sorted(data.effects, key=lambda effect: effect.apply_at_callsite):
            codeloc = data.callsite_codeloc if effect.apply_at_callsite else data.function_codeloc
            state.move_codelocs(codeloc)  # no-op if duplicated
            # mark uses
            for source in effect.sources_defns or set():
                if source.atom not in args_atoms_from_values:
                    state.add_use_by_def(source, expr=None)
            if effect.dest is None:
                continue

            value = effect.value if effect.value is not None else MultiValues(state.top(effect.dest.bits))
            # special case: if there is exactly one ret atom, we expect that the caller will do something
            # with the value, e.g. if this is a call expression.
            if data.caller_will_handle_single_ret and data.ret_atoms == {effect.dest}:
                data.ret_values = value
                data.ret_values_deps = effect.sources_defns
            else:
                # mark definition
                _, defs = state.kill_and_add_definition(
                    effect.dest,
                    value,
                    endness=None,
                    uses=effect.sources_defns or set(),
                    tags=effect.tags,
                )
                # categorize the output defn as either ret or other based on the atoms
                for defn in defs:
                    if data.ret_atoms is not None and defn.atom not in data.ret_atoms:
                        other_output_defns.add(defn)
                    else:
                        ret_defns.add(defn)

        # record this callsite
        state.analysis.function_calls[data.callsite_codeloc] = FunctionCallRelationships(
            callsite=data.callsite_codeloc,
            target=data.address,
            args_defns=args_defns,
            other_input_defns=other_input_defns,
            ret_defns=ret_defns,
            other_output_defns=other_output_defns,
        )
        # move the current codeloc back to the callsite
        state.move_codelocs(data.callsite_codeloc)

    def handle_generic_function(self, state: ReachingDefinitionsState, data: FunctionCallData):
        assert data.cc is not None
        assert data.prototype is not None
        if data.prototype.returnty is not None:
            if not isinstance(data.prototype.returnty, SimTypeBottom):
                data.ret_values = MultiValues(
                    state.top(data.prototype.returnty.with_arch(state.arch).size or state.arch.bits)
                )
            else:
                data.ret_values = MultiValues(state.top(state.arch.bits))
        if data.guessed_prototype:
            # use all!
            # TODO should we use some number of stack variables as well?
            if data.ret_atoms is not None:
                for ret_atom in data.ret_atoms:
                    data.depends(
                        ret_atom,
                        *(Register(*state.arch.registers[reg_name], arch=state.arch) for reg_name in data.cc.ARG_REGS),
                        apply_at_callsite=True,
                    )
        else:
            sources = {atom for arg in data.args_atoms or [] for atom in arg}
            if not data.ret_atoms:
                data.depends(None, *sources, apply_at_callsite=True)  # controversial
                return
            for atom in data.ret_atoms:
                data.depends(atom, *sources, apply_at_callsite=True)

    def handle_indirect_function(self, state: ReachingDefinitionsState, data: FunctionCallData) -> None:
        self.handle_generic_function(state, data)

    def handle_local_function(self, state: ReachingDefinitionsState, data: FunctionCallData) -> None:
        if self.interfunction_level > 0 and data.function is not None and state.analysis is not None:
            self.interfunction_level -= 1
            try:
                self.recurse_analysis(state, data)
            finally:
                self.interfunction_level += 1
        else:
            self.handle_generic_function(state, data)

    def handle_external_function(self, state: ReachingDefinitionsState, data: FunctionCallData) -> None:
        self.handle_generic_function(state, data)

    def recurse_analysis(self, state: ReachingDefinitionsState, data: FunctionCallData) -> None:
        """
        Precondition: ``data.function`` MUST NOT BE NONE in order to call this method.
        """
        assert state.analysis is not None
        assert data.function is not None

        # Set up the additional observation points of the return sites
        # They will be gathered and merged in get_exit_livedefinitions
        # get_exit_livedefinitions is currently only using ret_sites, but an argument could be made that it should
        # include jumpout sites as well. In the CFG generation tail call sites seem to be treated as return sites
        # and not as jumpout sites, so we are following that convention here.
        return_observation_points: list[ObservationPoint] = [
            (
                cast(Literal["node"], "node"),  # pycharm doesn't treat a literal string, as Literal[] by default...
                block.addr,
                ObservationPointType.OP_AFTER,
            )
            for block in data.function.ret_sites
        ]

        sub_rda = state.analysis.project.analyses.ReachingDefinitions(
            data.function,
            observe_all=state.analysis._observe_all,
            observation_points=list(state.analysis._observation_points or []) + return_observation_points,
            observe_callback=state.analysis._observe_callback,
            dep_graph=state.dep_graph,
            function_handler=self,
            init_state=state,
        )
        # migrate data from sub_rda to its parent
        state.analysis.function_calls.update(sub_rda.function_calls)
        state.analysis.model.observed_results.update(sub_rda.model.observed_results)
        state.all_definitions |= sub_rda.all_definitions

        sub_ld = get_exit_livedefinitions(data.function, sub_rda.model)
        if sub_ld is not None:
            state.live_definitions = sub_ld
        data.retaddr_popped = True

    @staticmethod
    def c_args_as_atoms(state: ReachingDefinitionsState, cc: SimCC, prototype: SimTypeFunction) -> list[set[Atom]]:
        if not prototype.variadic:
            sp_value = state.get_one_value(Register(state.arch.sp_offset, state.arch.bytes), strip_annotations=True)
            sp = state.get_stack_offset(sp_value) if sp_value is not None else None
            atoms = []
            for arg in cc.arg_locs(prototype):
                atoms_set = set()
                for footprint_arg in arg.get_footprint():
                    try:
                        atom = Atom.from_argument(
                            footprint_arg,
                            state.arch,
                            full_reg=True,
                            sp=sp,
                        )
                    except ValueError:
                        continue
                    atoms_set.add(atom)
                atoms.append(atoms_set)
            return atoms
        return [{Register(*state.arch.registers[arg_name], arch=state.arch)} for arg_name in cc.ARG_REGS]

    @staticmethod
    def c_return_as_atoms(state: ReachingDefinitionsState, cc: SimCC, prototype: SimTypeFunction) -> set[Atom]:
        if prototype.returnty is not None and not isinstance(prototype.returnty, SimTypeBottom):
            retval = cc.return_val(prototype.returnty)
            if retval is not None:
                return {
                    Atom.from_argument(footprint_arg, state.arch, full_reg=True)
                    for footprint_arg in retval.get_footprint()
                }
        return set()

    @staticmethod
    def caller_saved_regs_as_atoms(state: ReachingDefinitionsState, cc: SimCC) -> set[Register]:
        return (
            {Register(*state.arch.registers[reg], arch=state.arch) for reg in cc.CALLER_SAVED_REGS}
            if cc.CALLER_SAVED_REGS is not None
            else set()
        )

    @staticmethod
    def stack_pointer_as_atom(state) -> Register:
        return Register(state.arch.sp_offset, state.arch.bytes, state.arch)
