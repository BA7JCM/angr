from __future__ import annotations

import networkx
from angr.ailment.expression import VirtualVariable
from angr.ailment.statement import Assignment, Call, ConditionalJump

from angr.analyses import Analysis, register_analysis
from angr.utils.ail import is_head_controlled_loop_block, is_phi_assignment
from angr.utils.ssa import VVarUsesCollector, phi_assignment_get_src


class SLivenessModel:
    """
    The SLiveness model that stores LiveIn and LiveOut sets for each block in a partial-SSA function.
    """

    def __init__(self):
        self.live_ins = {}
        self.live_outs = {}


class SLivenessAnalysis(Analysis):
    """
    Calculates LiveIn and LiveOut sets for each block in a partial-SSA function.
    """

    def __init__(
        self,
        func,
        func_graph=None,
        entry=None,
        func_addr: int | None = None,
        arg_vvars: list[VirtualVariable] | None = None,
    ):
        self.func = func
        self.func_addr = func_addr if func_addr is not None else func.addr
        self.func_graph = func_graph if func_graph is not None else func.graph
        self.entry = (
            entry
            if entry is not None
            else next(iter(bb for bb in self.func_graph if bb.addr == self.func_addr and bb.idx is None))
        )
        self.arg_vvars = arg_vvars or []

        self.model = SLivenessModel()

        self._analyze()

    def _analyze(self):
        graph = self.func_graph
        entry = self.entry

        # initialize the live_in and live_out sets
        live_ins = {}
        live_outs = {}
        for block in graph.nodes():
            block_key = block.addr, block.idx
            live_ins[block_key] = set()
            live_outs[block_key] = set()

        live_on_edges: dict[tuple[tuple[int, int | None], tuple[int, int | None]], set[int]] = {}

        worklist = list(networkx.dfs_postorder_nodes(graph, source=entry))
        worklist_set = set(worklist)

        while worklist:
            block = worklist.pop(0)
            worklist_set.remove(block)

            block_key = block.addr, block.idx
            changed = False

            head_controlled_loop = is_head_controlled_loop_block(block)

            live = set()
            for succ in graph.successors(block):
                if head_controlled_loop and (block.addr, block.idx) == (succ.addr, succ.idx):
                    # this is a head-controlled loop block; we ignore the self-loop edge because all variables defined
                    # in the block after the conditional jump will be dead after leaving the current block
                    continue
                edge = (block.addr, block.idx), (succ.addr, succ.idx)
                if edge in live_on_edges:
                    live |= live_on_edges[edge]
                else:
                    live |= live_ins[(succ.addr, succ.idx)]

            if live != live_outs[block_key]:
                changed = True
                live_outs[block_key] = live.copy()

            if head_controlled_loop:
                # this is a head-controlled loop block; we start scanning from the first condition jump backwards
                condjump_idx = next(
                    iter(i for i, stmt in enumerate(block.statements) if isinstance(stmt, ConditionalJump)), None
                )
                assert condjump_idx is not None
                stmts = block.statements[: condjump_idx + 1]
            else:
                stmts = block.statements

            live_in_by_pred = {}
            for stmt in reversed(stmts):
                # handle assignments: a defined vvar is not live before the assignment
                if isinstance(stmt, Assignment) and isinstance(stmt.dst, VirtualVariable):
                    live.discard(stmt.dst.varid)
                elif isinstance(stmt, Call) and isinstance(stmt.ret_expr, VirtualVariable):
                    live.discard(stmt.ret_expr.varid)

                phi_expr = phi_assignment_get_src(stmt)
                if phi_expr is not None:
                    for src, vvar in phi_expr.src_and_vvars:
                        if head_controlled_loop and src == (block.addr, block.idx):
                            # this is a head-controlled loop block; we ignore the self-loop edge
                            continue

                        if src not in live_in_by_pred:
                            live_in_by_pred[src] = live.copy()
                        if vvar is not None:
                            live_in_by_pred[src].add(vvar.varid)
                        live_in_by_pred[src].discard(stmt.dst.varid)

                # handle the statement: add used vvars to the live set
                if head_controlled_loop and is_phi_assignment(stmt):
                    for src, vvar in stmt.src.src_and_vvars:
                        # this is a head-controlled loop block; we ignore the self-loop edge
                        if src != (block.addr, block.idx) and vvar is not None:
                            live |= {vvar.varid}
                else:
                    vvar_use_collector = VVarUsesCollector()
                    vvar_use_collector.walk_statement(stmt)
                    live |= vvar_use_collector.vvars

            if live_ins[block_key] != live:
                live_ins[block_key] = live
                changed = True

            for pred_addr, live in live_in_by_pred.items():
                key = pred_addr, block_key
                if key not in live_on_edges or live_on_edges[key] != live:
                    live_on_edges[key] = live
                    changed = True

            if changed:
                new_nodes = [
                    node for node in networkx.dfs_postorder_nodes(graph, source=block) if node not in worklist_set
                ]
                worklist.extend(new_nodes)
                worklist_set |= set(new_nodes)

        # set the model accordingly
        self.model.live_ins = live_ins
        self.model.live_outs = live_outs

    def interference_graph(self) -> networkx.Graph:
        """
        Generate an interference graph based on the liveness analysis result.

        :return: A networkx.Graph instance.
        """

        graph = networkx.Graph()

        for block in self.func_graph.nodes():
            live = self.model.live_outs[(block.addr, block.idx)].copy()

            if is_head_controlled_loop_block(block):
                # this is a head-controlled loop block; we start scanning from the first condition jump backwards
                condjump_idx = next(
                    iter(i for i, stmt in enumerate(block.statements) if isinstance(stmt, ConditionalJump)), None
                )
                assert condjump_idx is not None
                stmts = block.statements[: condjump_idx + 1]
            else:
                stmts = block.statements

            for stmt in reversed(stmts):
                if isinstance(stmt, Assignment) and isinstance(stmt.dst, VirtualVariable):
                    def_vvar = stmt.dst.varid
                elif isinstance(stmt, Call) and isinstance(stmt.ret_expr, VirtualVariable):
                    def_vvar = stmt.ret_expr.varid
                else:
                    def_vvar = None

                # handle the statement: add used vvars to the live set
                vvar_use_collector = VVarUsesCollector()
                vvar_use_collector.walk_statement(stmt)

                if def_vvar is not None:
                    for live_vvar in live:
                        graph.add_edge(def_vvar, live_vvar)
                    live.discard(def_vvar)
                live |= vvar_use_collector.vvars

            if block.addr == self.func_addr:
                # deal with function arguments
                for arg_vvar in self.arg_vvars:
                    for live_vvar in live:
                        graph.add_edge(arg_vvar.varid, live_vvar)

        return graph


register_analysis(SLivenessAnalysis, "SLiveness")
