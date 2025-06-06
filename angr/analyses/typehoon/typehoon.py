# pylint:disable=bad-builtin
from __future__ import annotations
from typing import TYPE_CHECKING
from collections import defaultdict

from angr.sim_type import SimStruct, SimTypePointer, SimTypeArray
from angr.errors import AngrRuntimeError
from angr.analyses.analysis import Analysis, AnalysesHub
from angr.sim_variable import SimVariable, SimStackVariable
from .simple_solver import SimpleSolver
from .translator import TypeTranslator
from .typeconsts import Struct, Pointer, TypeConstant, Array, TopType
from .typevars import Equivalence, Subtype, TypeVariable, DerivedTypeVariable

if TYPE_CHECKING:
    from angr.sim_type import SimType
    from .typevars import TypeConstraint


class Typehoon(Analysis):
    """
    A spiritual tribute to the long-standing typehoon project that @jmg (John Grosen) worked on during his days in the
    angr team. Now I feel really bad of asking the poor guy to work directly on VEX IR without any fancy static analysis
    support as we have right now...

    Typehoon analysis implements a pushdown system that simplifies and solves type constraints. Our type constraints are
    largely an implementation of the paper Polymorphic Type Inference for Machine Code by Noonan, Loginov, and Cok from
    GrammaTech (with missing functionality support and bugs, of course). Type constraints are collected by running
    VariableRecoveryFast (maybe VariableRecovery later as well) on a function, and then solved using this analysis.

    User may specify ground truth, which will override all types at certain program points during constraint solving.
    """

    def __init__(
        self,
        constraints,
        func_var,
        ground_truth=None,
        var_mapping: dict[SimVariable, set[TypeVariable]] | None = None,
        must_struct: set[TypeVariable] | None = None,
        stackvar_max_sizes: dict[TypeVariable, int] | None = None,
        stack_offset_tvs: dict[int, TypeVariable] | None = None,
    ):
        """

        :param constraints:
        :param ground_truth:        A set of SimType-style solutions for some or all type variables. They will be
                                    respected during type solving.
        :param var_mapping:
        :param must_struct:
        """

        self.func_var: TypeVariable = func_var
        self._constraints: dict[TypeVariable, set[TypeConstraint]] = constraints
        self._ground_truth: dict[TypeVariable, SimType] | None = ground_truth
        self._var_mapping = var_mapping
        self._must_struct = must_struct
        self._stackvar_max_sizes = stackvar_max_sizes if stackvar_max_sizes is not None else {}
        self._stack_offset_tvs = stack_offset_tvs if stack_offset_tvs is not None else {}

        self.bits = self.project.arch.bits
        self.solution = None
        self.structs = None
        self.simtypes_solution = None

        # stats
        self.processed_constraints_count: int = 0
        self.eqclass_constraints_count: list[int] = []

        # import pprint
        # pprint.pprint(self._var_mapping)
        # pprint.pprint(self._constraints)
        self._analyze()
        # pprint.pprint(self.solution)

    #
    # Public methods
    #

    def update_variable_types(
        self,
        func_addr: int | str,
        var_to_typevars: dict[SimVariable, set[TypeVariable]],
        stack_offset_tvs: dict[int, TypeVariable] | None = None,
    ) -> None:

        if not self.simtypes_solution:
            return

        for var, typevars in var_to_typevars.items():
            # if the variable is a stack variable, does the stack offset have any corresponding type variable?
            typevars_list = sorted(typevars, key=lambda tv: tv.idx)
            if stack_offset_tvs and isinstance(var, SimStackVariable) and var.offset in stack_offset_tvs:
                typevars_list.append(stack_offset_tvs[var.offset])

            type_candidates: list[SimType] = []
            for typevar in typevars_list:
                type_ = self.simtypes_solution.get(typevar, None)
                # print("{} -> {}: {}".format(var, typevar, type_))
                # Hack: if a global address is of a pointer type and it is not an array, we unpack the type
                if (
                    func_addr == "global"
                    and isinstance(type_, SimTypePointer)
                    and not isinstance(type_.pts_to, SimTypeArray)
                ):
                    type_ = type_.pts_to
                if type_ is not None:
                    type_candidates.append(type_)

            # determine the best type - this logic can be made better!
            if not type_candidates:
                continue
            if len(type_candidates) > 1:
                types_by_size: dict[int, list[SimType]] = defaultdict(list)
                for t in type_candidates:
                    if t.size is not None:
                        types_by_size[t.size].append(t)
                if not types_by_size:
                    # we only have BOT and TOP? damn
                    the_type = type_candidates[0]
                else:
                    max_size = max(types_by_size.keys())
                    the_type = types_by_size[max_size][0]  # TODO: Sort it
            else:
                the_type = type_candidates[0]

            self.kb.variables[func_addr].set_variable_type(
                var, the_type, name=the_type.name if isinstance(the_type, SimStruct) else None
            )

    def pp_constraints(self) -> None:
        """
        Pretty-print constraints between *variables* using the variable mapping.
        """
        if self._var_mapping is None:
            raise ValueError("Variable mapping does not exist.")

        typevar_to_var = {}
        for k, typevars in self._var_mapping.items():
            for tv in typevars:
                typevar_to_var[tv] = k

        print(f"### {sum(map(len, self._constraints.values()))} constraints")
        for func_var in self._constraints:
            print(f"{func_var}:")
            lst = []
            for constraint in self._constraints[func_var]:
                lst.append("    " + constraint.pp_str(typevar_to_var))
            lst = sorted(lst)
            print("\n".join(lst))
        print("### end of constraints ###")

    def pp_solution(self) -> None:
        """
        Pretty-print solutions using the variable mapping.
        """
        if self._var_mapping is None:
            raise ValueError("Variable mapping does not exist.")
        if self.solution is None:
            raise AngrRuntimeError("Please run type solver before calling pp_solution().")

        typevar_to_var = {}
        for k, typevars in self._var_mapping.items():
            for tv in typevars:
                typevar_to_var[tv] = k

        print(f"### {len(self.solution)} solutions")
        for typevar in sorted(self.solution.keys(), key=str):
            sol = self.solution[typevar]
            var_and_typevar = f"{typevar_to_var[typevar]} ({typevar})" if typevar in typevar_to_var else typevar
            print(f"    {var_and_typevar} -> {sol}")
        for stack_off, tv in self._stack_offset_tvs.items():
            print(f"    stack_{stack_off:#x} ({tv}) -> {self.solution[tv]}")
        print("### end of solutions ###")

    #
    # Private methods
    #

    def _analyze(self):
        # convert ground truth into constraints
        if self._ground_truth:
            translator = TypeTranslator(arch=self.project.arch)
            for tv, sim_type in self._ground_truth.items():
                self._constraints[self.func_var].add(Equivalence(tv, translator.simtype2tc(sim_type)))

        self._solve()
        self._specialize()
        self._translate_to_simtypes()

        # apply ground truth
        if self._ground_truth and self.simtypes_solution is not None:
            self.simtypes_solution.update(self._ground_truth)

    @staticmethod
    def _resolve_derived(tv):
        return tv.type_var if isinstance(tv, DerivedTypeVariable) else tv

    def _solve(self):
        typevars = set()
        if self._var_mapping:
            for variable_typevars in self._var_mapping.values():
                typevars |= variable_typevars
            typevars |= set(self._stack_offset_tvs.values())
        else:
            # collect type variables from constraints
            for constraint in self._constraints[self.func_var]:
                if isinstance(constraint, Subtype):
                    if isinstance(constraint.sub_type, TypeVariable):
                        typevars.add(self._resolve_derived(constraint.sub_type))
                    if isinstance(constraint.super_type, TypeVariable):
                        typevars.add(self._resolve_derived(constraint.super_type))

        solver = SimpleSolver(self.bits, self._constraints, typevars, stackvar_max_sizes=self._stackvar_max_sizes)
        self.solution = solver.solution
        self.processed_constraints_count = solver.processed_constraints_count
        self.eqclass_constraints_count = solver.eqclass_constraints_count

    def _specialize(self):
        """
        Heuristics to make types more natural and more readable.

        - structs where every element is of the same type will be converted to an array of that element type.
        """

        if not self.solution:
            return

        memo = set()
        for tv in list(self.solution.keys()):
            if self._must_struct and tv in self._must_struct:
                continue
            sol = self.solution[tv]
            specialized = self._specialize_struct(sol, memo=memo)
            if specialized is not None:
                self.solution[tv] = specialized
            else:
                memo.add(sol)

    def _specialize_struct(self, tc, memo: set | None = None):
        if isinstance(tc, Pointer):
            if memo is not None and tc in memo:
                return None
            specialized = self._specialize_struct(tc.basetype, memo={tc} if memo is None else memo | {tc})
            if specialized is None:
                return None
            return tc.new(specialized)

        if isinstance(tc, Struct) and tc.fields and min(tc.fields) >= 0:
            offsets: list[int] = sorted(tc.fields.keys())  # get a sorted list of offsets
            offset0 = offsets[0]
            field0: TypeConstant = tc.fields[offset0]

            if len(tc.fields) == 1 and 0 in tc.fields:
                return field0

            # are all fields the same?
            if (
                len(tc.fields) > 1
                and not self._is_pointer_to(field0, tc)
                and all(tc.fields[off] == field0 for off in offsets)
            ):
                # are all fields aligned properly?
                try:
                    alignment = field0.size
                except NotImplementedError:
                    alignment = 1
                if all(off % alignment == 0 for off in offsets):
                    # yeah!
                    max_offset = offsets[-1]
                    field0_size = 1
                    if not isinstance(field0, TopType):
                        try:
                            field0_size = field0.size
                        except NotImplementedError:
                            field0_size = 1
                    count = (max_offset + field0_size) // alignment
                    return Array(field0, count=count)

        return None

    @staticmethod
    def _is_pointer_to(pointer_to: TypeConstant, base_type: TypeConstant) -> bool:
        return isinstance(pointer_to, Pointer) and pointer_to.basetype == base_type

    def _translate_to_simtypes(self):
        """
        Translate solutions in type variables to solutions in SimTypes.
        """

        if not self.solution:
            return

        simtypes_solution = {}
        translator = TypeTranslator(arch=self.project.arch)
        needs_backpatch = set()

        for tv, sol in self.solution.items():
            simtypes_solution[tv], has_nonexistent_ref = translator.tc2simtype(sol)
            if has_nonexistent_ref:
                needs_backpatch.add(tv)

        # back patch
        for tv in needs_backpatch:
            translator.backpatch(simtypes_solution[tv], simtypes_solution)

        self.simtypes_solution = simtypes_solution
        self.structs = translator.structs


AnalysesHub.register_default("Typehoon", Typehoon)
