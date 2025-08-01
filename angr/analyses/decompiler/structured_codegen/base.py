# pylint:disable=missing-class-docstring
from __future__ import annotations
from sortedcontainers import SortedDict

from angr.sim_variable import SimVariable

#
#   Position Mapping Classes
#


class PositionMappingElement:
    __slots__ = ("length", "obj", "start")

    def __init__(self, start, length, obj):
        self.start: int = start
        self.length: int = length
        self.obj = obj

    def __contains__(self, offset):
        return self.start <= offset < self.start + self.length

    def __repr__(self):
        return f"<{self.start}-{self.start + self.length}: {self.obj}>"


class PositionMapping:
    __slots__ = ("_posmap",)

    DUPLICATION_CHECK = True

    def __init__(self):
        self._posmap: SortedDict | dict[int, PositionMappingElement] = SortedDict()

    def items(self):
        return self._posmap.items()

    #
    # Public methods
    #

    def add_mapping(self, start_pos, length, obj):
        # duplication check
        if self.DUPLICATION_CHECK:
            try:
                pre = next(self._posmap.irange(maximum=start_pos, reverse=True))
                if start_pos in self._posmap[pre]:
                    raise ValueError("New mapping is overlapping with an existing element.")
            except StopIteration:
                pass

        self._posmap[start_pos] = PositionMappingElement(start_pos, length, obj)

    def get_node(self, pos: int):
        element = self.get_element(pos)
        if element is None:
            return None
        return element.obj

    def get_element(self, pos: int) -> PositionMappingElement | None:
        try:
            pre = next(self._posmap.irange(maximum=pos, reverse=True))
        except StopIteration:
            return None

        element = self._posmap[pre]
        if pos in element:
            return element
        return None


class InstructionMappingElement:
    __slots__ = ("ins_addr", "posmap_pos")

    def __init__(self, ins_addr, posmap_pos):
        self.ins_addr: int = ins_addr
        self.posmap_pos: int = posmap_pos

    def __contains__(self, offset: int):
        return self.ins_addr == offset

    def __repr__(self):
        return f"<{self.ins_addr}: {self.posmap_pos}>"


class InstructionMapping:
    __slots__ = ("_insmap",)

    def __init__(self):
        self._insmap: SortedDict | dict[int, InstructionMappingElement] = SortedDict()

    def items(self):
        return self._insmap.items()

    def add_mapping(self, ins_addr, posmap_pos):
        if ins_addr in self._insmap:
            if posmap_pos <= self._insmap[ins_addr].posmap_pos:
                self._insmap[ins_addr] = InstructionMappingElement(ins_addr, posmap_pos)
        else:
            self._insmap[ins_addr] = InstructionMappingElement(ins_addr, posmap_pos)

    def get_nearest_pos(self, ins_addr: int) -> int | None:
        try:
            pre_max = next(self._insmap.irange(maximum=ins_addr, reverse=True))
            pre_min = next(self._insmap.irange(minimum=ins_addr, reverse=True))
        except StopIteration:
            return None

        e1: InstructionMappingElement = self._insmap[pre_max]
        e2: InstructionMappingElement = self._insmap[pre_min]

        if abs(ins_addr - e1.ins_addr) <= abs(ins_addr - e2.ins_addr):
            return e1.posmap_pos
        return e2.posmap_pos


class BaseStructuredCodeGenerator:
    def __init__(self, flavor=None, notes=None):
        self.flavor = flavor
        self.text = None
        self.map_pos_to_node = None
        self.map_pos_to_addr = None
        self.map_addr_to_pos = None
        self.map_ast_to_pos: dict[SimVariable, set[PositionMappingElement]] | None = None
        self.notes = notes if notes is not None else {}

    def adjust_mapping_positions(
        self,
        offset: int,
        pos_to_node: PositionMapping,
        pos_to_addr: PositionMapping,
        addr_to_pos: InstructionMapping,
    ) -> tuple[PositionMapping, PositionMapping, InstructionMapping]:
        """
        Adjust positions in the mappings to account for the notes that are prepended to the text.

        :param offset:       The length of the notes to prepend.
        :param pos_to_node:  The position to node mapping.
        :param pos_to_addr:  The position to address mapping.
        :param addr_to_pos:  The address to position mapping.
        :return:             Adjusted mappings.
        """
        new_pos_to_node = PositionMapping()
        new_pos_to_addr = PositionMapping()
        new_addr_to_pos = InstructionMapping()

        for pos, node in pos_to_node.items():
            new_pos_to_node.add_mapping(pos + offset, node.length, node.obj)

        for pos, node in pos_to_addr.items():
            new_pos_to_addr.add_mapping(pos + offset, node.length, node.obj)

        for addr, pos in addr_to_pos.items():
            new_addr_to_pos.add_mapping(addr, pos.posmap_pos + offset)

        return new_pos_to_node, new_pos_to_addr, new_addr_to_pos

    def reapply_options(self, options):
        pass

    def regenerate_text(self) -> None:
        pass

    def reload_variable_types(self) -> None:
        pass
