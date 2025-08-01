# pylint:disable=too-many-boolean-expressions
from __future__ import annotations
import os
import logging
import itertools
from collections import defaultdict
from collections.abc import Iterable
import contextlib
from typing import overload

import networkx
import pydemumble

from cle.backends.symbol import Symbol
from archinfo.arch_arm import get_real_address_if_arm
import claripy

from angr.knowledge_plugins.cfg.memory_data import MemoryDataSort
from angr.codenode import CodeNode, BlockNode, HookNode, SyscallNode
from angr.serializable import Serializable
from angr.errors import AngrValueError, SimEngineError, SimMemoryError
from angr.procedures import SIM_LIBRARIES
from angr.procedures.definitions import SimSyscallLibrary
from angr.protos import function_pb2
from angr.calling_conventions import DEFAULT_CC, default_cc
from angr.sim_type import SimTypeFunction, parse_defns
from angr.calling_conventions import SimCC
from angr.project import Project
from angr.utils.library import get_cpp_function_name
from .function_parser import FunctionParser

l = logging.getLogger(name=__name__)


class Function(Serializable):
    """
    A representation of a function and various information about it.
    """

    __slots__ = (
        "_addr_to_block_node",
        "_argument_registers",
        "_argument_stack_variables",
        "_block_sizes",
        "_call_sites",
        "_callout_sites",
        "_cyclomatic_complexity",
        "_endpoints",
        "_function_manager",
        "_jumpout_sites",
        "_local_block_addrs",
        "_local_blocks",
        "_local_transition_graph",
        "_name",
        "_project",
        "_ret_sites",
        "_retout_sites",
        "_returning",
        "addr",
        "addr",
        "binary_name",
        "bp_on_stack",
        "calling_convention",
        "from_signature",
        "info",
        "is_alignment",
        "is_default_name",
        "is_plt",
        "is_prototype_guessed",
        "is_simprocedure",
        "is_syscall",
        "normalized",
        "previous_names",
        "prototype",
        "prototype_libname",
        "ran_cca",
        "retaddr_on_stack",
        "sp_delta",
        "startpoint",
        "tags",
        "transition_graph",
    )

    def __init__(
        self,
        function_manager,
        addr,
        name=None,
        syscall=None,
        is_simprocedure: bool | None = None,
        binary_name=None,
        is_plt: bool | None = None,
        returning=None,
        alignment=False,
        calling_convention: SimCC | None = None,
        prototype: SimTypeFunction | None = None,
        prototype_libname: str | None = None,
        is_prototype_guessed: bool = True,
    ):
        """
        Function constructor. If the optional parameters are not provided, they will be automatically determined upon
        the creation of a Function object.

        :param addr:            The address of the function.

        The following parameters are optional.

        :param str name:        The name of the function.
        :param bool syscall:    Whether this function is a syscall or not.
        :param bool is_simprocedure:    Whether this function is a SimProcedure or not.
        :param str binary_name: Name of the binary where this function is.
        :param bool is_plt:     If this function is a PLT entry.
        :param bool returning:  If this function returns.
        :param bool alignment:  If this function acts as an alignment filler. Such functions usually only contain nops.
        """
        self.transition_graph = networkx.classes.digraph.DiGraph()
        self._local_transition_graph = None
        self.normalized = False

        # block nodes at whose ends the function returns
        self._ret_sites: set[BlockNode] = set()
        # block nodes at whose ends the function jumps out to another function (jumps outside)
        self._jumpout_sites: set[BlockNode] = set()
        # block nodes at whose ends the function calls out to another non-returning function
        self._callout_sites: set[BlockNode] = set()
        # block nodes that ends the function by returning out to another function (returns outside). This is rare.
        self._retout_sites: set[BlockNode] = set()
        # block nodes (basic block nodes) at whose ends the function terminates
        # in theory, if everything works fine, endpoints == ret_sites | jumpout_sites | callout_sites
        self._endpoints: defaultdict[str, set[BlockNode]] = defaultdict(set)

        self._call_sites = {}
        self.addr = addr
        # startpoint can be None if the corresponding CFGNode is a syscall node
        self.startpoint = None
        self._function_manager = function_manager
        self.is_syscall = None
        self.is_simprocedure = False
        self.is_alignment = alignment

        # These properties are set by VariableManager
        self.bp_on_stack = False
        self.retaddr_on_stack = False
        self.sp_delta = 0
        # Calling convention
        self.calling_convention = calling_convention
        # Function prototype
        self.prototype = prototype
        self.prototype_libname = prototype_libname
        self.is_prototype_guessed = is_prototype_guessed
        # Whether this function returns or not. `None` means it's not determined yet
        self._returning = None

        self._addr_to_block_node = {}  # map addresses to nodes. it's a cache of blocks. if a block is removed from the
        # function, it may not be removed from _addr_to_block_node. if you want to list
        # all blocks of a function, access .blocks.
        self._block_sizes = {}  # map addresses to block sizes
        self._local_blocks = {}  # a dict of all blocks inside the function
        self._local_block_addrs = set()  # a set of addresses of all blocks inside the function

        self.info = {}  # storing special information, like $gp values for MIPS32
        self.tags = ()  # store function tags. can be set manually by performing CodeTagging analysis.

        # Initialize _cyclomatic_complexity to None
        self._cyclomatic_complexity = None

        # TODO: Can we remove the following two members?
        # Register offsets of those arguments passed in registers
        self._argument_registers = []
        # Stack offsets of those arguments passed in stack variables
        self._argument_stack_variables = []

        self._project: Project | None = None  # will be initialized upon the first access to self.project

        self.ran_cca = False  # this is set by CompleteCallingConventions to avoid reprocessing failed functions

        #
        # Initialize unspecified properties
        #

        if syscall is not None:
            self.is_syscall = syscall
        else:
            if self.project is None:
                raise ValueError(
                    "'syscall' must be specified if you do not specify a function manager for this new function."
                )

            # Determine whether this function is a syscall or not
            self.is_syscall = self.project.simos.is_syscall_addr(addr)

        # Determine whether this function is a SimProcedure
        if is_simprocedure is not None:
            self.is_simprocedure = is_simprocedure
        else:
            if self.project is None:
                raise ValueError(
                    "'is_simprocedure' must be specified if you do not specify a function manager for this new "
                    "function."
                )

            if self.is_syscall or self.project.is_hooked(addr):
                self.is_simprocedure = True

        # Determine if this function is a PLT entry
        if is_plt is not None:
            self.is_plt = is_plt
        else:
            if self._function_manager is not None:
                # use the faster cached version
                self.is_plt = self._function_manager.is_plt_cached(addr)
            else:
                # Whether this function is a PLT entry or not is primarily relying on the PLT detection in CLE; it may
                # also be updated (to True) during CFG recovery.
                if self.project is None:
                    raise ValueError(
                        "'is_plt' must be specified if you do not specify a function manager for this new function."
                    )
                self.is_plt = self.project.loader.find_plt_stub_name(addr) is not None

        # Determine the name of this function
        if name is None:
            self._name = self._get_initial_name()
        else:
            self.is_default_name = False
            self._name = name
        self.previous_names = []
        self.from_signature: str | None = None

        # Determine the name the binary where this function is.
        if binary_name is not None:
            self.binary_name = binary_name
        else:
            self.binary_name = self._get_initial_binary_name()

        # Determine returning status for SimProcedures and Syscalls
        if returning is not None:
            self._returning = returning
        else:
            if self.project is None:
                raise ValueError(
                    "'returning' must be specified if you do not specify a function manager for this new function."
                )

            self._returning = self._get_initial_returning()

        self._init_prototype_and_calling_convention()

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, v):
        self.previous_names.append(self._name)
        self._name = v
        self._function_manager._kb.labels[self.addr] = v

    @property
    def project(self):
        if self._project is None and self._function_manager is not None:
            # try to set it from function manager
            self._project: Project | None = self._function_manager._kb._project
        return self._project

    @property
    def returning(self):
        return self._returning

    @returning.setter
    def returning(self, v):
        self._returning = v

    @property
    def blocks(self):
        """
        An iterator of all local blocks in the current function.

        :return: angr.lifter.Block instances.
        """

        for block_addr, block in self._local_blocks.items():
            with contextlib.suppress(SimEngineError, SimMemoryError):
                yield self.get_block(
                    block_addr, size=block.size, byte_string=block.bytestr if isinstance(block, BlockNode) else None
                )

    @property
    def cyclomatic_complexity(self):
        """
        The cyclomatic complexity of the function.

        Cyclomatic complexity is a software metric used to indicate the complexity of a program.
        It is a quantitative measure of the number of linearly independent paths through a program's source code.
        It is computed using the formula: M = E - N + 2P, where
        E = the number of edges in the graph,
        N = the number of nodes in the graph,
        P = the number of connected components.

        The cyclomatic complexity value is lazily computed and cached for future use.
        Initially this value is None until it is computed for the first time

        :return: The cyclomatic complexity of the function.
        :rtype: int
        """
        if self._cyclomatic_complexity is None:
            self._cyclomatic_complexity = (
                self.transition_graph.number_of_edges() - self.transition_graph.number_of_nodes() + 2
            )
        return self._cyclomatic_complexity

    @property
    def xrefs(self):
        """
        An iterator of all xrefs of the current function.

        :return: angr.knowledge_plugins.xrefs.xref.XRef instances.
        """
        for block in self.blocks:
            yield from self._function_manager._kb.xrefs.get_xrefs_by_ins_addr_region(
                block.addr, block.addr + block.size
            )

    @property
    def block_addrs(self):
        """
        An iterator of all local block addresses in the current function.

        :return: block addresses.
        """

        return self._local_blocks.keys()

    @property
    def block_addrs_set(self):
        """
        Return a set of block addresses for a better performance of inclusion tests.

        :return: A set of block addresses.
        :rtype: set
        """

        return self._local_block_addrs

    def get_block(self, addr: int, size: int | None = None, byte_string: bytes | None = None):
        """
        Getting a block out of the current function.

        :param int addr:    The address of the block.
        :param int size:    The size of the block. This is optional. If not provided, angr will load
        :param byte_string:
        :return:
        """
        if size is None and addr in self.block_addrs:
            # we know the size
            size = self._block_sizes[addr]

        assert self.project is not None
        block = self.project.factory.block(addr, size=size, byte_string=byte_string)
        if size is None:
            # update block_size dict
            self._block_sizes[addr] = block.size
        return block

    # compatibility
    _get_block = get_block

    def get_block_size(self, addr: int) -> int | None:
        return self._block_sizes.get(addr, None)

    @property
    def nodes(self) -> Iterable[CodeNode]:
        return self.transition_graph.nodes()

    def get_node(self, addr) -> BlockNode | None:
        return self._addr_to_block_node.get(addr, None)

    @property
    def has_unresolved_jumps(self):
        for addr in self.block_addrs:
            if addr in self._function_manager._kb.unresolved_indirect_jumps:
                b = self._function_manager._kb._project.factory.block(addr)
                if b.vex.jumpkind == "Ijk_Boring":
                    return True
        return False

    @property
    def has_unresolved_calls(self):
        for addr in self.block_addrs:
            if addr in self._function_manager._kb.unresolved_indirect_jumps:
                b = self._function_manager._kb._project.factory.block(addr)
                if b.vex.jumpkind == "Ijk_Call":
                    return True
        return False

    @property
    def operations(self):
        """
        All of the operations that are done by this functions.
        """
        return [op for block in self.blocks for op in block.vex.operations]

    @property
    def code_constants(self):
        """
        All of the constants that are used by this functions's code.
        """
        # TODO: remove link register values
        return [const.value for block in self.blocks for const in block.vex.constants]

    @classmethod
    def _get_cmsg(cls):
        return function_pb2.Function()  # pylint:disable=no-member

    def serialize_to_cmessage(self):
        return FunctionParser.serialize(self)

    @classmethod
    def parse_from_cmessage(cls, cmsg, **kwargs):
        """
        :param cmsg:

        :return Function: The function instantiated out of the cmsg data.
        """
        return FunctionParser.parse_from_cmsg(cmsg, **kwargs)

    def string_references(self, minimum_length=2):
        """
        All of the constant string references used by this function.

        :param minimum_length:  The minimum length of strings to find (default is 1)
        :return:                A generator yielding tuples of (address, string) where is address
                                is the location of the string in memory.
        """

        cfg = self._function_manager._kb.cfgs.get_most_accurate()

        for x in self.xrefs:
            try:
                md = cfg.memory_data[x.dst]
            except KeyError:
                continue
            if md.sort not in {MemoryDataSort.String, MemoryDataSort.UnicodeString}:
                continue
            if len(md.content) < minimum_length:
                continue

            yield (md.addr, md.content)

    @property
    def local_runtime_values(self):
        """
        Tries to find all runtime values of this function which do not come from inputs.
        These values are generated by starting from a blank state and reanalyzing the basic blocks once each.
        Function calls are skipped, and back edges are never taken so these values are often unreliable,
        This function is good at finding simple constant addresses which the function will use or calculate.

        :return: a set of constants
        """
        constants = set()

        assert self.project is not None
        if not self.project.loader.main_object.contains_addr(self.addr):
            return constants

        # FIXME the old way was better for architectures like mips, but we need the initial irsb
        # reanalyze function with a new initial state (use persistent registers)
        # initial_state = self._function_manager._cfg.get_any_irsb(self.addr).initial_state
        # fresh_state = self.project.factory.blank_state(mode="fastpath")
        # for reg in initial_state.arch.persistent_regs + ['ip']:
        #     fresh_state.registers.store(reg, initial_state.registers.load(reg))

        # reanalyze function with a new initial state
        fresh_state = self.project.factory.blank_state(mode="fastpath")
        fresh_state.regs.ip = self.addr

        graph_addrs = {x.addr for x in self.graph.nodes() if isinstance(x, BlockNode)}

        # process the nodes in a breadth-first order keeping track of which nodes have already been analyzed
        analyzed = set()
        q = [fresh_state]
        analyzed.add(fresh_state.solver.eval(fresh_state.ip))
        while len(q) > 0:
            state = q.pop()
            # make sure its in this function
            if state.solver.eval(state.ip) not in graph_addrs:
                continue
            # don't trace into simprocedures
            if self.project.is_hooked(state.solver.eval(state.ip)):
                continue
            # don't trace outside of the binary
            if not self.project.loader.main_object.contains_addr(state.solver.eval(state.ip)):
                continue
            # don't trace unreachable blocks
            if state.history.jumpkind in {
                "Ijk_EmWarn",
                "Ijk_NoDecode",
                "Ijk_MapFail",
                "Ijk_NoRedir",
                "Ijk_SigTRAP",
                "Ijk_SigSEGV",
                "Ijk_ClientReq",
            }:
                continue

            curr_ip = state.solver.eval(state.ip)

            # get runtime values from logs of successors
            successors = self.project.factory.successors(state)
            for succ in successors.flat_successors + successors.unsat_successors:
                for a in succ.history.recent_actions:
                    for ao in a.all_objects:
                        if not isinstance(ao.ast, claripy.ast.Base):
                            constants.add(ao.ast)
                        elif not ao.ast.symbolic:
                            constants.add(succ.solver.eval(ao.ast))

                # add successors to the queue to analyze
                if not succ.solver.symbolic(succ.ip):
                    succ_ip = succ.solver.eval(succ.ip)
                    if succ_ip in self and succ_ip not in analyzed:
                        analyzed.add(succ_ip)
                        q.insert(0, succ)

            # force jumps to missing successors
            # (this is a slightly hacky way to force it to explore all the nodes in the function)
            node = self.get_node(curr_ip)
            if node is None:
                # the node does not exist. maybe it's not a block node.
                continue
            missing = {x.addr for x in list(self.graph.successors(node))} - analyzed
            for succ_addr in missing:
                l.info("Forcing jump to missing successor: %#x", succ_addr)
                if succ_addr not in analyzed:
                    all_successors = (
                        successors.unconstrained_successors + successors.flat_successors + successors.unsat_successors
                    )
                    if len(all_successors) > 0:
                        # set the ip of a copied successor to the successor address
                        succ = all_successors[0].copy()
                        succ.ip = succ_addr
                        analyzed.add(succ_addr)
                        q.insert(0, succ)
                    else:
                        l.warning("Could not reach successor: %#x", succ_addr)

        return constants

    @property
    def num_arguments(self):
        return len(self._argument_registers) + len(self._argument_stack_variables)

    def __contains__(self, val):
        if isinstance(val, int):
            return val in self._block_sizes
        return False

    def __str__(self):
        return (
            f"Function {self.name} [{self.addr:#x}]\n"
            f"  Syscall: {self.is_syscall}\n"
            f"  SP difference: {self.sp_delta}\n"
            f"  Has return: {self.has_return}\n"
            f"  Returning: {'Unknown' if self.returning is None else self.returning}\n"
            f"  Alignment: {self.is_alignment}\n"
            f"  Arguments: reg: {self._argument_registers}, stack: {self._argument_stack_variables}\n"
            f"  Blocks: [{', '.join(f'{i:#x}' for i in self.block_addrs)}]\n"
            f"  Cyclomatic Complexity: {self.cyclomatic_complexity}\n"
            f"  Calling convention: {self.calling_convention}"
        )

    def __repr__(self):
        if self.is_syscall:
            return f"<Syscall function {self.name} ({hex(self.addr) if isinstance(self.addr, int) else self.addr})>"
        return f"<Function {self.name} ({hex(self.addr) if isinstance(self.addr, int) else self.addr})>"

    def __setstate__(self, state):
        for k, v in state.items():
            setattr(self, k, v)

    def __getstate__(self):
        # self._local_transition_graph is a cache. don't pickle it
        d = {k: getattr(self, k) for k in self.__slots__}
        d["_local_transition_graph"] = None
        d["_project"] = None
        d["_function_manager"] = None
        return d

    @property
    def endpoints(self):
        return list(itertools.chain(*self._endpoints.values()))

    @property
    def endpoints_with_type(self):
        return self._endpoints

    @property
    def ret_sites(self):
        return list(self._ret_sites)

    @property
    def jumpout_sites(self):
        return list(self._jumpout_sites)

    @property
    def retout_sites(self):
        return list(self._retout_sites)

    @property
    def callout_sites(self):
        return list(self._callout_sites)

    @property
    def size(self):
        return sum(self._block_sizes[addr] for addr in self._local_blocks)

    @property
    def binary(self):
        """
        Get the object this function belongs to.
        :return: The object this function belongs to.
        """
        assert self.project is not None
        return self.project.loader.find_object_containing(self.addr, membership_check=False)

    @property
    def offset(self) -> int:
        """
        :return: the function's binary offset (i.e., non-rebased address)
        """
        return self.addr - self.binary.mapped_base

    @property
    def symbol(self) -> None | Symbol:
        """
        :return: the function's Symbol, if any
        """
        return self.binary.loader.find_symbol(self.addr)

    @property
    def pseudocode(self) -> str | None:
        """
        :return: the function's pseudocode
        """
        dec = self.project.analyses.Decompiler(self, cfg=self._function_manager._kb.cfgs.get_most_accurate())
        return dec.codegen.text if dec.codegen else None

    def add_jumpout_site(self, node: CodeNode):
        """
        Add a custom jumpout site.

        :param node:    The address of the basic block that control flow leaves during this transition.
        :return:        None
        """

        node = self._register_node(True, node)
        self._jumpout_sites.add(node)
        self._add_endpoint(node, "transition")

    def add_retout_site(self, node: CodeNode):
        """
        Add a custom retout site.

        Retout (returning to outside of the function) sites are very rare. It mostly occurs during CFG recovery when we
        incorrectly identify the beginning of a function in the first iteration, and then correctly identify that
        function later in the same iteration (function alignments can lead to this bizarre case). We will mark all edges
        going out of the header of that function as a outside edge, because all successors now belong to the
        incorrectly-identified function. This identification error will be fixed in the second iteration of CFG
        recovery. However, we still want to keep track of jumpouts/retouts during the first iteration so other logic in
        CFG recovery still work.

        :param node: The address of the basic block that control flow leaves the current function after a call.
        :return:     None
        """

        node = self._register_node(True, node)
        self._retout_sites.add(node)
        self._add_endpoint(node, "return")

    def _get_initial_name(self):
        """
        Determine the most suitable name of the function.

        :return:    The initial function name.
        :rtype:     string
        """

        name = None
        addr = self.addr

        self.is_default_name = False
        # Try to get a name from existing labels
        if self._function_manager is not None and addr in self._function_manager._kb.labels:
            name = self._function_manager._kb.labels[addr]

        # try to get the name from a hook
        if name is None and self.project is not None:
            project = self.project
            if project.is_hooked(addr):
                hooker = project.hooked_by(addr)
                if hooker is not None:
                    name = hooker.display_name
            elif project.simos.is_syscall_addr(addr):
                syscall_inst = project.simos.syscall_from_addr(addr)
                if syscall_inst is not None:
                    name = syscall_inst.display_name

        # generate an IDA-style sub_X name
        if name is None:
            self.is_default_name = True
            name = f"sub_{addr:x}"

        return name

    def _get_initial_binary_name(self) -> str | None:
        """
        Determine the name of the binary where this function is.

        :return: None
        """

        binary_name = None

        # if this function is a simprocedure but not a syscall, use its library name as
        # its binary name
        # if it is a syscall, fall back to use self.binary.binary which explicitly says cle##kernel
        if self.project and self.is_simprocedure and not self.is_syscall:
            hooker = self.project.hooked_by(self.addr)
            if hooker is not None:
                binary_name = hooker.library_name

        if binary_name is None:
            if self._function_manager is not None:
                # use the faster cached version
                binary_name = self._function_manager.get_binary_name_cached(self.addr)
            else:
                if self.binary is not None and self.binary.binary:
                    binary_name = os.path.basename(self.binary.binary)

        return binary_name

    def _get_initial_returning(self):
        """
        Determine if this function returns or not *if it is hooked by a SimProcedure or a user hook*.

        :return:    True if the hooker returns, False otherwise.
        :rtype:     bool
        """

        hooker = None
        if self.is_syscall:
            hooker = self.project.simos.syscall_from_addr(self.addr)
        elif self.is_simprocedure:
            hooker = self.project.hooked_by(self.addr)
        if hooker:
            if hasattr(hooker, "DYNAMIC_RET") and hooker.DYNAMIC_RET:
                return True
            if hasattr(hooker, "NO_RET"):
                return not hooker.NO_RET

        # Cannot determine
        return None

    def _init_prototype_and_calling_convention(self) -> None:
        """
        Initialize prototype and calling convention from a SimProcedure, if available.
        """
        hooker = None
        if self.is_syscall and self.project is not None and self.project.simos.is_syscall_addr(self.addr):
            hooker = self.project.simos.syscall_from_addr(self.addr)
        elif self.is_simprocedure and self.project is not None:
            hooker = self.project.hooked_by(self.addr)
        if hooker is None or hooker.guessed_prototype:
            return

        if hooker.prototype:
            self.prototype_libname = hooker.library_name
            self.prototype = hooker.prototype
            self.is_prototype_guessed = False

        cc = hooker.cc
        if cc is None and self.project is not None:
            arch = self.project.arch
            if arch.name in DEFAULT_CC:
                cc_cls = default_cc(
                    arch.name, platform=self.project.simos.name if self.project.simos is not None else None
                )
                if cc_cls is not None:
                    cc = cc_cls(arch)
        self.calling_convention = cc

    def _clear_transition_graph(self):
        self._block_sizes = {}
        self._addr_to_block_node = {}
        self._local_blocks = {}
        self._local_block_addrs = set()
        self.startpoint = None
        self.transition_graph = networkx.classes.digraph.DiGraph()
        self._local_transition_graph = None

        self._ret_sites = set()
        self._jumpout_sites = set()
        self._callout_sites = set()
        self._retout_sites = set()
        self._endpoints = defaultdict(set)
        self._call_sites = {}

    def _confirm_fakeret(self, src, dst):
        if src not in self.transition_graph or dst not in self.transition_graph[src]:
            raise AngrValueError(f"FakeRet edge ({src}, {dst}) is not in transition graph.")

        data = self.transition_graph[src][dst]

        if "type" not in data or data["type"] != "fake_return":
            raise AngrValueError(f"Edge ({src}, {dst}) is not a FakeRet edge")

        # it's confirmed. register the node if needed
        if "outside" not in data or data["outside"] is False:
            dst = self._register_node(True, dst)

        self.transition_graph[src][dst]["confirmed"] = True

    def _transit_to(
        self, from_node: CodeNode, to_node, outside=False, ins_addr=None, stmt_idx=None, is_exception=False
    ):
        """
        Registers an edge between basic blocks in this function's transition graph.
        Arguments are CodeNode objects.

        :param from_node            The address of the basic block that control
                                    flow leaves during this transition.
        :param to_node              The address of the basic block that control
                                    flow enters during this transition.
        :param bool outside:        If this is a transition to another function, e.g. tail call optimization
        :return: None
        """

        if outside:
            from_node = self._register_node(True, from_node)
            if to_node is not None:
                to_node = self._register_node(False, to_node)

            self._jumpout_sites.add(from_node)
        else:
            from_node = self._register_node(True, from_node)
            if to_node is not None:
                to_node = self._register_node(True, to_node)

        type_ = "transition" if not is_exception else "exception"
        if to_node is not None:
            self.transition_graph.add_edge(
                from_node, to_node, type=type_, outside=outside, ins_addr=ins_addr, stmt_idx=stmt_idx
            )

        if outside:
            # this node is an endpoint of the current function
            self._add_endpoint(from_node, type_)

        # clear the cache
        self._local_transition_graph = None

    def _call_to(self, from_node, to_func, ret_node, stmt_idx=None, ins_addr=None, return_to_outside=False):
        """
        Registers an edge between the caller basic block and callee function.

        :param from_addr:   The basic block that control flow leaves during the transition.
        :type  from_addr:   angr.knowledge.CodeNode
        :param to_func:     The function that we are calling
        :type  to_func:     Function
        :param ret_node     The basic block that control flow should return to after the
                            function call.
        :type  to_func:     angr.knowledge.CodeNode or None
        :param stmt_idx:    Statement ID of this call.
        :type  stmt_idx:    int, str or None
        :param ins_addr:    Instruction address of this call.
        :type  ins_addr:    int or None
        """

        from_node = self._register_node(True, from_node)

        if to_func.is_syscall:
            self.transition_graph.add_edge(from_node, to_func, type="syscall", stmt_idx=stmt_idx, ins_addr=ins_addr)
        else:
            self.transition_graph.add_edge(from_node, to_func, type="call", stmt_idx=stmt_idx, ins_addr=ins_addr)
            if ret_node is not None:
                ret_node = self._register_node(return_to_outside is False, ret_node)
                self._fakeret_to(from_node, ret_node, to_outside=return_to_outside)

        self._local_transition_graph = None

    def _fakeret_to(self, from_node, to_node, confirmed=None, to_outside=False):
        from_node = self._register_node(True, from_node)
        if confirmed:
            to_node = self._register_node(not to_outside, to_node)

        if confirmed is None:
            self.transition_graph.add_edge(from_node, to_node, type="fake_return", outside=to_outside)
        else:
            self.transition_graph.add_edge(
                from_node, to_node, type="fake_return", confirmed=confirmed, outside=to_outside
            )

        self._local_transition_graph = None

    def _remove_fakeret(self, from_node, to_node):
        self.transition_graph.remove_edge(from_node, to_node)

        self._local_transition_graph = None

    def _return_from_call(self, from_func, to_node, to_outside=False):
        self.transition_graph.add_edge(from_func, to_node, type="return", to_outside=to_outside)
        for _, _, data in self.transition_graph.in_edges(to_node, data=True):
            if "type" in data and data["type"] == "fake_return":
                data["confirmed"] = True

        self._local_transition_graph = None

    def _update_local_blocks(self, node: CodeNode):
        if node.addr not in self._local_blocks or self._local_blocks[node.addr] != node:
            self._local_blocks[node.addr] = node
            self._local_block_addrs.add(node.addr)

    def _update_addr_to_block_cache(self, node: BlockNode):
        if node.addr not in self._addr_to_block_node:
            self._addr_to_block_node[node.addr] = node

    @overload
    def _register_node(self, is_local: bool, node: CodeNode) -> CodeNode: ...

    @overload
    def _register_node(self, is_local: bool, node: Function) -> Function: ...

    def _register_node(self, is_local: bool, node: CodeNode | Function) -> CodeNode | Function:
        # if the node already exists and is the same, we reuse the existing node
        if is_local and self._local_blocks.get(node.addr, None) == node:
            return self._local_blocks[node.addr]

        if node.addr not in self and node not in self.transition_graph:
            # only add each node to the graph once
            self.transition_graph.add_node(node)

        if not isinstance(node, CodeNode):
            # function and other things bail here
            return node

        # this is either a new node or a different node at the same address
        node._graph = self.transition_graph
        if self._block_sizes.get(node.addr, 0) == 0:
            self._block_sizes[node.addr] = node.size
        if node.addr == self.addr and (self.startpoint is None or not self.startpoint.is_hook):
            self.startpoint = node
        if is_local and node.addr not in self._local_blocks:
            self._update_local_blocks(node)
        # add BlockNodes to the addr_to_block_node cache if not already there
        if isinstance(node, BlockNode):
            self._update_addr_to_block_cache(node)
            # else:
            #    # checks that we don't have multiple block nodes at a single address
            #    assert node == self._addr_to_block_node[node.addr]
        return node

    def _add_return_site(self, return_site: CodeNode):
        """
        Registers a basic block as a site for control flow to return from this function.

        :param return_site:     The block node that ends with a return.
        """
        return_site = self._register_node(True, return_site)

        self._ret_sites.add(return_site)
        # A return site must be an endpoint of the function - you cannot continue execution of the current function
        # after returning
        self._add_endpoint(return_site, "return")

    def _add_call_site(self, call_site_addr, call_target_addr, retn_addr):
        """
        Registers a basic block as calling a function and returning somewhere.

        :param call_site_addr:       The address of a basic block that ends in a call.
        :param call_target_addr:     The address of the target of said call.
        :param retn_addr:            The address that said call will return to.
        """
        self._call_sites[call_site_addr] = (call_target_addr, retn_addr)

    def _add_endpoint(self, endpoint_node, sort):
        """
        Registers an endpoint with a type of `sort`. The type can be one of the following:
        - call: calling a function that does not return
        - return: returning from the current function
        - transition: a jump/branch targeting a different function

        It is possible for a block to act as two different sorts of endpoints. For example, consider the following
        block:

        .text:0000000000024350                 mov     eax, 1
        .text:0000000000024355                 lock xadd [rdi+4], eax
        .text:000000000002435A                 retn

        VEX code:
           00 | ------ IMark(0x424350, 5, 0) ------
           01 | PUT(rax) = 0x0000000000000001
           02 | PUT(rip) = 0x0000000000424355
           03 | ------ IMark(0x424355, 5, 0) ------
           04 | t11 = GET:I64(rdi)
           05 | t10 = Add64(t11,0x0000000000000004)
           06 | t0 = LDle:I32(t10)
           07 | t2 = Add32(t0,0x00000001)
           08 | t(4,4294967295) = CASle(t10 :: (t0,None)->(t2,None))
           09 | t14 = CasCmpNE32(t4,t0)
           10 | if (t14) { PUT(rip) = 0x424355; Ijk_Boring }
           11 | PUT(cc_op) = 0x0000000000000003
           12 | t15 = 32Uto64(t0)
           13 | PUT(cc_dep1) = t15
           14 | PUT(cc_dep2) = 0x0000000000000001
           15 | t17 = 32Uto64(t0)
           16 | PUT(rax) = t17
           17 | PUT(rip) = 0x000000000042435a
           18 | ------ IMark(0x42435a, 1, 0) ------
           19 | t6 = GET:I64(rsp)
           20 | t7 = LDle:I64(t6)
           21 | t8 = Add64(t6,0x0000000000000008)
           22 | PUT(rsp) = t8
           23 | t18 = Sub64(t8,0x0000000000000080)
           24 | ====== AbiHint(0xt18, 128, t7) ======
           NEXT: PUT(rip) = t7; Ijk_Ret

        This block acts as both a return endpoint and a transition endpoint (transitioning to 0x424355).

        :param endpoint_node:       The endpoint node.
        :param sort:                Type of the endpoint.
        :return:                    None
        """

        self._endpoints[sort].add(endpoint_node)

    def mark_nonreturning_calls_endpoints(self):
        """
        Iterate through all call edges in transition graph. For each call a non-returning function, mark the source
        basic block as an endpoint.

        This method should only be executed once all functions are recovered and analyzed by CFG recovery, so we know
        whether each function returns or not.

        :return: None
        """

        for src, dst, data in self.transition_graph.edges(data=True):
            if "type" in data and data["type"] == "call":
                func_addr = dst.addr
                if func_addr in self._function_manager:
                    function = self._function_manager[func_addr]
                    if function.returning is False:
                        # the target function does not return
                        the_node = self.get_node(src.addr)
                        if the_node is not None:
                            self._callout_sites.add(the_node)
                            self._add_endpoint(the_node, "call")

    def get_call_sites(self) -> Iterable[int]:
        """
        Gets a list of all the basic blocks that end in calls.

        :return:                    A view of the addresses of the blocks that end in calls.
        """
        return self._call_sites.keys()

    def get_call_target(self, callsite_addr):
        """
        Get the target of a call.

        :param callsite_addr:       The address of a basic block that ends in a call.
        :return:                    The target of said call, or None if callsite_addr is not a
                                    callsite.
        """
        if callsite_addr in self._call_sites:
            return self._call_sites[callsite_addr][0]
        return None

    def get_call_return(self, callsite_addr):
        """
        Get the hypothetical return address of a call.

        :param callsite_addr:       The address of the basic block that ends in a call.
        :return:                    The likely return target of said call, or None if callsite_addr
                                    is not a callsite.
        """
        if callsite_addr in self._call_sites:
            return self._call_sites[callsite_addr][1]
        return None

    @property
    def graph(self):
        """
        Get a local transition graph. A local transition graph is a transition graph that only contains nodes that
        belong to the current function. All edges, except for the edges going out from the current function or coming
        from outside the current function, are included.

        The generated graph is cached in self._local_transition_graph.

        :return:    A local transition graph.
        :rtype:     networkx.DiGraph
        """

        if self._local_transition_graph is not None:
            return self._local_transition_graph

        g = networkx.classes.digraph.DiGraph()
        if self.startpoint is not None:
            g.add_node(self.startpoint)
        for block in self._local_blocks.values():
            g.add_node(block)
        for src, dst, data in self.transition_graph.edges(data=True):
            if "type" in data and (
                (data["type"] in ("transition", "exception") and ("outside" not in data or data["outside"] is False))
                or (data["type"] == "fake_return" and ("outside" not in data or data["outside"] is False))
            ):
                g.add_edge(src, dst, **data)

        self._local_transition_graph = g

        return g

    def graph_ex(self, exception_edges=True):
        """
        Get a local transition graph with a custom configuration. A local transition graph is a transition graph that
        only contains nodes that belong to the current function. This method allows user to exclude certain types of
        edges together with the nodes that are only reachable through such edges, such as exception edges.

        The generated graph is not cached.

        :param bool exception_edges:    Should exception edges and the nodes that are only reachable through exception
                                        edges be kept.
        :return:                        A local transition graph with a special configuration.
        :rtype:                         networkx.DiGraph
        """

        # graph_ex() should not impact any already cached graph
        old_cached_graph = self._local_transition_graph
        graph = self.graph
        self._local_transition_graph = old_cached_graph  # restore the cached graph

        # fast path
        if exception_edges:
            return graph

        # BFS on local graph but ignoring certain types of graphs
        g = networkx.classes.digraph.DiGraph()
        queue = [n for n in graph if n is self.startpoint or graph.in_degree[n] == 0]
        traversed = set(queue)

        while queue:
            node = queue.pop(0)

            g.add_node(node)
            for _, dst, edge_data in graph.out_edges(node, data=True):
                edge_type = edge_data.get("type", None)
                if not exception_edges and edge_type == "exception":
                    # ignore this edge
                    continue
                g.add_edge(node, dst, **edge_data)

                if dst not in traversed:
                    traversed.add(dst)
                    queue.append(dst)

        return g

    def transition_graph_ex(self, exception_edges=True):
        """
        Get a transition graph with a custom configuration. This method allows user to exclude certain types of edges
        together with the nodes that are only reachable through such edges, such as exception edges.

        The generated graph is not cached.

        :param bool exception_edges:    Should exception edges and the nodes that are only reachable through exception
                                        edges be kept.
        :return:                        A local transition graph with a special configuration.
        :rtype:                         networkx.DiGraph
        """

        graph = self.transition_graph

        # fast path
        if exception_edges:
            return graph

        # BFS on local graph but ignoring certain types of graphs
        g = networkx.classes.digraph.DiGraph()
        queue = [n for n in graph if n is self.startpoint or graph.in_degree[n] == 0]
        traversed = set(queue)

        while queue:
            node = queue.pop(0)
            traversed.add(node)

            g.add_node(node)
            for _, dst, edge_data in graph.out_edges(node, data=True):
                edge_type = edge_data.get("type", None)
                if not exception_edges and edge_type == "exception":
                    # ignore this edge
                    continue
                g.add_edge(node, dst, **edge_data)

                if dst not in traversed:
                    traversed.add(dst)
                    queue.append(dst)

        return g

    def subgraph(self, ins_addrs):
        """
        Generate a sub control flow graph of instruction addresses based on self.graph

        :param iterable ins_addrs: A collection of instruction addresses that should be included in the subgraph.
        :return networkx.DiGraph: A subgraph.
        """

        # find all basic blocks that include those instructions
        blocks = []
        block_addr_to_insns = {}

        for b in self._local_blocks.values():
            # TODO: should I call get_blocks?
            block = self.get_block(b.addr, size=b.size, byte_string=b.bytestr)
            common_insns = set(block.instruction_addrs).intersection(ins_addrs)
            if common_insns:
                blocks.append(b)
                block_addr_to_insns[b.addr] = sorted(common_insns)

        # subgraph = networkx.subgraph(self.graph, blocks)
        subgraph = self.graph.subgraph(blocks).copy()
        g = networkx.classes.digraph.DiGraph()

        for n in subgraph.nodes():
            insns = block_addr_to_insns[n.addr]

            in_edges = subgraph.in_edges(n)
            # out_edges = subgraph.out_edges(n)
            # the first instruction address should be included
            if len(in_edges) > 1 and n.addr not in insns:
                insns = [n.addr, *insns]

            for src, _ in in_edges:
                last_instr = block_addr_to_insns[src.addr][-1]
                g.add_edge(last_instr, insns[0])

            for i in range(len(insns) - 1):
                g.add_edge(insns[i], insns[i + 1])

        return g

    def instruction_size(self, insn_addr):
        """
        Get the size of the instruction specified by `insn_addr`.

        :param int insn_addr: Address of the instruction
        :return int: Size of the instruction in bytes, or None if the instruction is not found.
        """

        for block in self.blocks:
            if insn_addr in block.instruction_addrs:
                index = block.instruction_addrs.index(insn_addr)
                if index == len(block.instruction_addrs) - 1:
                    # the very last instruction
                    size = block.addr + block.size - insn_addr
                else:
                    size = block.instruction_addrs[index + 1] - insn_addr
                return size

        return None

    def addr_to_instruction_addr(self, addr):
        """
        Obtain the address of the instruction that covers @addr.

        :param int addr:    An address.
        :return:            Address of the instruction that covers @addr, or None if this addr is not covered by any
                            instruction of this function.
        :rtype:             int or None
        """

        # TODO: Replace the linear search with binary search
        for b in self.blocks:
            if b.addr <= addr < b.addr + b.size:
                # found it
                for i, instr_addr in enumerate(b.instruction_addrs):
                    if (i < len(b.instruction_addrs) - 1 and instr_addr <= addr < b.instruction_addrs[i + 1]) or (
                        i == len(b.instruction_addrs) - 1 and instr_addr <= addr
                    ):
                        return instr_addr
                # Not covered by any instruction... why?
                return None
        return None

    def dbg_print(self):
        """
        Returns a representation of the list of basic blocks in this function.
        """
        return "[{}]".format(", ".join((f"{n.addr:#08x}") for n in self.transition_graph.nodes()))

    def dbg_draw(self, filename):
        """
        Draw the graph and save it to a PNG file.
        """
        import matplotlib.pyplot as pyplot  # pylint: disable=import-error,import-outside-toplevel
        from networkx.drawing.nx_agraph import graphviz_layout  # pylint: disable=import-error,import-outside-toplevel

        tmp_graph = networkx.classes.digraph.DiGraph()
        for from_block, to_block in self.transition_graph.edges():
            node_a = f"{from_block.addr:#08x}"
            node_b = f"{to_block.addr:#08x}"
            if node_b in self._ret_sites:
                node_b += "[Ret]"
            if node_a in self._call_sites:
                node_a += "[Call]"
            tmp_graph.add_edge(node_a, node_b)
        pos = graphviz_layout(tmp_graph, prog="fdp")  # pylint: disable=no-member
        networkx.draw(tmp_graph, pos, node_size=1200)
        pyplot.savefig(filename)

    def _add_argument_register(self, reg_offset):
        """
        Registers a register offset as being used as an argument to the function.

        :param reg_offset:          The offset of the register to register.
        """
        if reg_offset in self._function_manager._arg_registers and reg_offset not in self._argument_registers:
            self._argument_registers.append(reg_offset)

    def _add_argument_stack_variable(self, stack_var_offset):
        if stack_var_offset not in self._argument_stack_variables:
            self._argument_stack_variables.append(stack_var_offset)

    @property
    def arguments(self):
        if self.calling_convention is None:
            return self._argument_registers + self._argument_stack_variables
        if self.prototype is None:
            return []
        return self.calling_convention.arg_locs(self.prototype)

    @property
    def has_return(self):
        return len(self._ret_sites) > 0

    @property
    def callable(self):
        assert self.project is not None
        return self.project.factory.callable(self.addr)

    def normalize(self):
        """
        Make sure all basic blocks in the transition graph of this function do not overlap. You will end up with a CFG
        that IDA Pro generates.

        This method does not touch the CFG result. You may call CFG{Emulated, Fast}.normalize() for that matter.

        :return: None
        """
        assert self.project is not None

        # let's put a check here
        if self.startpoint is None:
            # this function is empty
            l.debug("Unexpected error: %s does not have any blocks. normalize() fails.", repr(self))
            return

        graph = self.transition_graph
        end_addresses: defaultdict[int, list[BlockNode]] = defaultdict(list)

        for block in self.nodes:
            if isinstance(block, BlockNode):
                end_addr = block.addr + block.size
                end_addresses[end_addr].append(block)

        while any(len(x) > 1 for x in end_addresses.values()):
            end_addr, all_nodes = next((end_addr, x) for (end_addr, x) in end_addresses.items() if len(x) > 1)

            all_nodes = sorted(all_nodes, key=lambda node: node.size)
            smallest_node = all_nodes[0]
            other_nodes = all_nodes[1:]

            is_outside_node = False
            if smallest_node not in graph:
                is_outside_node = True

            # Break other nodes
            for n in other_nodes:
                new_size = get_real_address_if_arm(self.project.arch, smallest_node.addr) - get_real_address_if_arm(
                    self.project.arch, n.addr
                )
                if new_size == 0:
                    # This is the node that has the same size as the smallest one
                    continue

                new_end_addr = n.addr + new_size

                # Does it already exist?
                new_node = None
                if new_end_addr in end_addresses:
                    nodes = [i for i in end_addresses[new_end_addr] if i.addr == n.addr]
                    if len(nodes) > 0:
                        new_node = nodes[0]

                if new_node is None:
                    # TODO: Do this correctly for hook nodes
                    # Create a new one
                    new_node = BlockNode(n.addr, new_size, graph=graph, thumb=n.thumb)
                    self._block_sizes[n.addr] = new_size
                    self._addr_to_block_node[n.addr] = new_node
                    # Put the newnode into end_addresses
                    end_addresses[new_end_addr].append(new_node)

                # Modify the CFG
                original_predecessors = list(graph.in_edges([n], data=True))
                original_successors = list(graph.out_edges([n], data=True))

                for _, d, data in original_successors:
                    ins_addr = data.get("ins_addr", None)
                    if ins_addr is not None and ins_addr < d.addr:
                        continue
                    if d not in graph[smallest_node]:
                        if d is n:
                            graph.add_edge(smallest_node, new_node, **data)
                        else:
                            graph.add_edge(smallest_node, d, **data)

                for p, _, _ in original_predecessors:
                    graph.remove_edge(p, n)
                graph.remove_node(n)

                # update local_blocks
                if n.addr in self._local_blocks and self._local_blocks[n.addr].size != new_node.size:
                    del self._local_blocks[n.addr]
                    self._local_blocks[n.addr] = new_node

                # update block_cache and block_sizes
                if n.addr in self._block_sizes and self._block_sizes[n.addr] != new_node.size:
                    # the cache needs updating
                    self._block_sizes[n.addr] = new_node.size

                for p, _, data in original_predecessors:
                    if p not in other_nodes:
                        graph.add_edge(p, new_node, **data)

                # We should find the correct successor
                new_successors = [i for i in all_nodes if i.addr == smallest_node.addr]
                if new_successors:
                    new_successor = new_successors[0]
                    new_ins_addrs = self.project.factory.block(new_node.addr, size=new_node.size).instruction_addrs
                    if self.project.arch.branch_delay_slot and len(new_ins_addrs) >= 2:
                        new_ins_addr = new_ins_addrs[-2]
                    elif len(new_ins_addrs) >= 1:
                        new_ins_addr = new_ins_addrs[-1]
                    else:
                        # the new node is somehow not decode-able
                        new_ins_addr = new_node.addr + new_node.size - 1
                    graph.add_edge(
                        new_node,
                        new_successor,
                        type="transition",
                        outside=is_outside_node,
                        ins_addr=new_ins_addr,
                    )
                else:
                    # We gotta create a new one
                    l.error("normalize(): Please report it to Fish.")

                # update endpoints
                for sortset in self._endpoints.values():
                    if n in sortset:
                        sortset.remove(n)
                        sortset.add(smallest_node)

            end_addresses[end_addr] = [smallest_node]

        # Rebuild startpoint
        if self.startpoint.size != self._block_sizes[self.startpoint.addr]:
            self.startpoint = self.get_node(self.startpoint.addr)

        # Clear the cache
        self._local_transition_graph = None

        self.normalized = True

    def find_declaration(self, ignore_binary_name: bool = False, binary_name_hint: str | None = None) -> bool:
        """
        Find the most likely function declaration from the embedded collection of prototypes, set it to self.prototype,
        and update self.calling_convention with the declaration.

        :param ignore_binary_name:  Do not rely on the executable or library where the function belongs to determine
                                    its source library. This is useful when working on statically linked binaries
                                    (because all functions will belong to the main executable). We will search for all
                                    libraries in angr to find the first declaration match.
        :param binary_name_hint:    Substring of the library name where this function might be originally coming from.
                                    Useful for FLIRT-identified functions in statically linked binaries.
        :return:                    True if a declaration is found and self.prototype and self.calling_convention are
                                    updated. False if we fail to find a matching function declaration, in which case
                                    self.prototype or self.calling_convention will be kept untouched.
        """

        if not ignore_binary_name:
            # determine the library name
            if not self.is_plt:
                binary_name = self.binary_name
                if binary_name not in SIM_LIBRARIES:
                    return False
            else:
                binary_name = None
                # PLT entries must have the same declaration as their jump targets
                # Try to determine which library this PLT entry will jump to
                edges = self.transition_graph.edges()
                if len(edges) == 0:
                    return False
                node = next(iter(edges))[1]
                if len(edges) == 1 and (type(node) is HookNode or type(node) is SyscallNode):
                    target = node.addr
                    if target in self._function_manager:
                        target_func = self._function_manager[target]
                        binary_name = target_func.binary_name

            # cannot determine the binary name. since we are forced to respect binary name, we give up in this case.
            if binary_name is None:
                return False

            lib = SIM_LIBRARIES.get(binary_name, None)
            libraries = set()
            if lib is not None:
                libraries.update(lib)

        else:
            # try all libraries or all libraries that match the given library name hint
            libraries = set()
            for lib_name, libs in SIM_LIBRARIES.items():
                # TODO: Add support for syscall libraries. Note that syscall libraries have different function
                #  prototypes for .has_prototype() and .get_prototype()...
                for lib in libs:
                    if not isinstance(lib, SimSyscallLibrary):
                        if binary_name_hint:
                            if binary_name_hint.lower() in lib_name.lower():
                                libraries.add(lib)
                        else:
                            libraries.add(lib)

        if not libraries:
            return False

        name_variants = [self.name]
        # remove "_" prefixes
        if self.name.startswith("_"):
            name_variants.append(self.name[1:])
        if self.name.startswith("__"):
            name_variants.append(self.name[2:])
        # special handling for libc
        if self.name.startswith("__libc_"):
            name_variants.append(self.name[7:])

        for library in libraries:
            for name in name_variants:
                if isinstance(library, SimSyscallLibrary):
                    # FIXME: we don't support getting declaration from a syscall library yet. we don't have the concept
                    # of abi at this point.
                    continue
                if not library.has_prototype(name):
                    continue

                proto = library.get_prototype(name)
                if self.project is None:
                    # we need to get arch from self.project
                    l.warning(
                        "Function %s does not have .project set. A possible prototype is found, but we cannot set it "
                        "without .project.arch."
                    )
                    return False
                self.prototype = proto.with_arch(self.project.arch)
                self.prototype_libname = library.name

                # update self.calling_convention if necessary
                if self.calling_convention is None:
                    if self.project.arch.name in library.default_ccs:
                        self.calling_convention = library.default_ccs[self.project.arch.name](self.project.arch)
                    elif self.project.arch.name in DEFAULT_CC:
                        self.calling_convention = default_cc(
                            self.project.arch.name,
                            platform=self.project.simos.name if self.project.simos is not None else None,
                        )(self.project.arch)

                return True

        return False

    @staticmethod
    def _addr_to_funcloc(addr):
        # FIXME
        if isinstance(addr, tuple):
            return addr[0]
        # int, long
        return addr

    def is_rust_function(self):
        ast = pydemumble.demangle(self.name)
        if ast:
            nodes = ast.split("::")
            if len(nodes) >= 2:
                last_node = nodes[-1]
                return (
                    len(last_node) == 17
                    and last_node.startswith("h")
                    and all(c in "0123456789abcdef" for c in last_node[1:])
                )
        return False

    @staticmethod
    def _rust_fmt_node(node):
        result = []
        rest = node
        if rest.startswith("_$"):
            rest = rest[1:]
        while True:
            if rest.startswith("."):
                if len(rest) > 1 and rest[1] == ".":
                    result.append("::")
                    rest = rest[2:]
                else:
                    result.append(".")
                    rest = rest[1:]
            elif rest.startswith("$"):
                if "$" in rest[1:]:
                    escape, rest = rest[1:].split("$", 1)
                else:
                    break

                unescaped = {"SP": "@", "BP": "*", "RF": "&", "LT": "<", "GT": ">", "LP": "(", "RP": ")", "C": ","}.get(
                    escape
                )

                if unescaped is None and escape.startswith("u"):
                    digits = escape[1:]
                    if all(c in "0123456789abcdef" for c in digits):
                        c = chr(int(digits, 16))
                        if ord(c) >= 32 and ord(c) != 127:
                            result.append(c)
                            continue
                if unescaped:
                    result.append(unescaped)
                else:
                    break
            else:
                idx = min((rest.find(c) for c in "$." if c in rest), default=len(rest))
                result.append(rest[:idx])
                rest = rest[idx:]
                if not rest:
                    break
        return "".join(result)

    @property
    def demangled_name(self):
        ast = pydemumble.demangle(self.name)
        if self.is_rust_function():
            nodes = ast.split("::")[:-1]
            ast = "::".join([Function._rust_fmt_node(node) for node in nodes])
        return ast if ast else self.name

    @property
    def short_name(self):
        if self.is_rust_function():
            ast = pydemumble.demangle(self.name)
            return Function._rust_fmt_node(ast.split("::")[-2])
        func_name = get_cpp_function_name(self.demangled_name)
        return func_name.split("::")[-1]

    def get_unambiguous_name(self, display_name: str | None = None) -> str:
        """
        Get a disambiguated function name.

        :param display_name: Name to display, otherwise the function name.
        :return: The function name in the form:
            ::<name>           when the function binary is the main object.
            ::<obj>::<name>    when the function binary is not the main object.
            ::<addr>::<name>   when the function binary is an unnamed non-main object, or when multiple functions with
                               the same name are defined in the function binary.
        """
        assert self.project is not None
        must_disambiguate_by_addr = self.binary is not self.project.loader.main_object and self.binary_name is None

        # If there are multiple functions with the same name in the same object, disambiguate by address
        if not must_disambiguate_by_addr:
            for func in self._function_manager.get_by_name(self.name):
                if func is not self and func.binary is self.binary:
                    must_disambiguate_by_addr = True
                    break

        separator = "::"
        n = separator
        if must_disambiguate_by_addr:
            n += hex(self.addr) + separator
        elif self.binary is not self.project.loader.main_object and self.binary_name is not None:
            n += self.binary_name + separator
        return n + (display_name or self.name)

    def apply_definition(self, definition: str, calling_convention: SimCC | type[SimCC] | None = None) -> None:
        assert self.project is not None
        if not definition.endswith(";"):
            definition += ";"
        func_def = parse_defns(definition, arch=self.project.arch)
        if len(func_def.keys()) > 1:
            raise Exception(f"Too many definitions: {list(func_def.keys())} ")

        name: str
        ty: SimTypeFunction
        name, ty = func_def.popitem()
        self.name = name
        self.prototype = ty.with_arch(self.project.arch)
        # setup the calling convention
        # If a SimCC object is passed assume that this is sane and just use it
        if isinstance(calling_convention, SimCC):
            self.calling_convention = calling_convention

        # If it is a subclass of SimCC we can instantiate it
        elif isinstance(calling_convention, type) and issubclass(calling_convention, SimCC):
            self.calling_convention = calling_convention(self.project.arch)

        # If none is specified default to something
        elif calling_convention is None:
            self.calling_convention = self.project.factory.cc()

        else:
            raise TypeError("calling_convention has to be one of: [SimCC, type(SimCC), None]")

    def functions_reachable(self) -> set[Function]:
        """
        :return: The set of all functions that can be reached from the function represented by self.
        """
        called = set()

        def _find_called(function_address):
            successors = set(self._function_manager.callgraph.successors(function_address)) - called
            for s in successors:
                called.add(s)
                _find_called(s)

        _find_called(self.addr)
        return {self._function_manager.function(a) for a in called}

    def copy(self):
        func = Function(self._function_manager, self.addr, name=self.name, syscall=self.is_syscall)
        func.transition_graph = networkx.DiGraph(self.transition_graph)
        func.normalized = self.normalized
        func._ret_sites = self._ret_sites.copy()
        func._jumpout_sites = self._jumpout_sites.copy()
        func._retout_sites = self._retout_sites.copy()
        func._endpoints = self._endpoints.copy()
        func._call_sites = self._call_sites.copy()
        func._project = self._project
        func.previous_names = list(self.previous_names)
        func.is_plt = self.is_plt
        func.is_simprocedure = self.is_simprocedure
        func.binary_name = self.binary_name
        func.bp_on_stack = self.bp_on_stack
        func.retaddr_on_stack = self.retaddr_on_stack
        func.sp_delta = self.sp_delta
        func.calling_convention = self.calling_convention
        func.prototype = self.prototype
        func._returning = self._returning
        func.is_alignment = self.is_alignment
        func.startpoint = self.startpoint
        func._addr_to_block_node = self._addr_to_block_node.copy()
        func._block_sizes = self._block_sizes.copy()
        func._local_blocks = self._local_blocks.copy()
        func._local_block_addrs = self._local_block_addrs.copy()
        func.info = self.info.copy()
        func.tags = self.tags

        return func

    def pp(self, **kwargs):
        """
        Pretty-print the function disassembly.
        """
        print(self.project.analyses.Disassembly(self).render(**kwargs))
