from __future__ import annotations
import logging

from capstone.x86_const import X86_OP_MEM

from .resolver import IndirectJumpResolver

l = logging.getLogger(name=__name__)


class X86PeIatResolver(IndirectJumpResolver):
    """
    A timeless indirect jump resolver for IAT in x86 PEs and xbes.
    """

    def __init__(self, project):
        super().__init__(project, timeless=True)

    def filter(self, cfg, addr, func_addr, block, jumpkind):
        if jumpkind not in {"Ijk_Call", "Ijk_Boring"}:  # both call and jmp
            return False

        insns = self.project.factory.block(addr).capstone.insns
        if not insns:
            return False
        if not insns[-1].insn.operands:
            return False

        opnd = insns[-1].insn.operands[0]
        # Must be of the form: call ds:0xABCD
        return bool(opnd.type == X86_OP_MEM and opnd.mem.disp and not opnd.mem.base and not opnd.mem.index)

    def resolve(
        self, cfg, addr, func_addr, block, jumpkind, func_graph_complete: bool = True, **kwargs
    ):  # pylint:disable=unused-argument
        slot = self.project.factory.block(addr).capstone.insns[-1].insn.disp
        target = cfg._fast_memory_load_pointer(slot)
        if target is None:
            l.warning("Address %#x does not appear to be mapped", slot)
            return False, []

        if not self.project.is_hooked(target):
            return False, []

        dest = self.project.hooked_by(target)
        l.debug("Resolved target to %s", dest.display_name)
        return True, [target]
