from __future__ import annotations
from angr.ailment.statement import Assignment
from angr.ailment.expression import BinaryOp, Const, Tmp

from .base import PeepholeOptimizationStmtBase
from .utils import get_expr_shift_left_amount


class RolRorRewriter(PeepholeOptimizationStmtBase):
    """
    Rewrites consecutive statements into ROL (rotate shift left) or ROR (rotate shift right) statements.
    """

    __slots__ = ()

    NAME = "ROL/ROR rewriter"
    stmt_classes = (Assignment,)

    def optimize(self, stmt: Assignment, stmt_idx: int | None = None, block=None, **kwargs):
        # Rol example:
        #    61 | t304 = Shr32(t301,0x19)
        #    62 | t306 = Shl32(t301,0x07)
        #    63 | t303 = Or32(t306,t304)
        #
        # Ror example:
        #    98 | 0x140002a06 | t453 = (Conv(64->32, r9<8>) << 0x11<8>)
        #    99 | 0x140002a06 | t455 = (Conv(64->32, r9<8>) >> 0xf<8>)
        #    100 | 0x140002a06 | t452 = (t455 | t453)
        #
        # Another Ror example (fully propagated):
        #        | tXXX = ((t301 << 0x7<8>) | (t301 >> 0x19<8>))
        if not (isinstance(stmt.src, BinaryOp) and stmt.src.op == "Or"):
            return None

        op0, op1 = stmt.src.operands
        if isinstance(op0, Tmp) and isinstance(op1, Tmp):
            if stmt_idx < 2:
                return None

            # check the previous two instructions
            stmt_1 = block.statements[stmt_idx - 1]
            stmt_2 = block.statements[stmt_idx - 2]
            if not (isinstance(stmt_1, Assignment) and isinstance(stmt_1.src, BinaryOp)):
                return None
            if not (isinstance(stmt_2, Assignment) and isinstance(stmt_2.src, BinaryOp)):
                return None

            if not isinstance(stmt_1.dst, Tmp):
                return None
            if not isinstance(stmt_2.dst, Tmp):
                return None

            if {stmt_1.dst.tmp_idx, stmt_2.dst.tmp_idx} != {op0.tmp_idx, op1.tmp_idx}:
                return None

            stmt1_op0, stmt1_op1 = stmt_1.src.operands
            stmt2_op0, stmt2_op1 = stmt_2.src.operands

            if not (stmt1_op0.likes(stmt2_op0)):
                return None

            if not (isinstance(stmt1_op1, Const) and isinstance(stmt2_op1, Const)):
                return None

            if (
                stmt_1.src.op in {"Shl", "Mul"}
                and stmt_2.src.op == "Shr"
                and (shiftleft_amount := get_expr_shift_left_amount(stmt_1.src)) is not None
                and shiftleft_amount + stmt2_op1.value == stmt.dst.bits
            ):
                rol_amount = Const(None, None, shiftleft_amount, 8, **stmt1_op1.tags)
                return Assignment(
                    stmt.idx,
                    stmt.dst,
                    BinaryOp(None, "Rol", [stmt1_op0, rol_amount], False, bits=stmt.dst.bits, **stmt_1.src.tags),
                    **stmt.tags,
                )
            if (
                stmt_1.src.op == "Shr"
                and stmt_2.src.op in {"Shl", "Mul"}
                and (shiftleft_amount := get_expr_shift_left_amount(stmt_2.src)) is not None
                and stmt1_op1.value + shiftleft_amount == stmt.dst.bits
            ):
                return Assignment(
                    stmt.idx,
                    stmt.dst,
                    BinaryOp(None, "Ror", [stmt1_op0, stmt1_op1], False, bits=stmt.dst.bits, **stmt_1.src.tags),
                    **stmt.tags,
                )
        elif (
            isinstance(op0, BinaryOp)
            and isinstance(op1, BinaryOp)
            and {op0.op, op1.op} in [{"Shl", "Shr"}, {"Mul", "Shr"}]
        ):
            if not op0.operands[0].likes(op1.operands[0]):
                return None

            if not isinstance(op0.operands[1], Const) or not isinstance(op1.operands[1], Const):
                return None
            op0_v = op0.operands[1].value
            op1_v = op1.operands[1].value

            if (
                op0.op in {"Shl", "Mul"}
                and op1.op == "Shr"
                and (op0_shiftamount := get_expr_shift_left_amount(op0)) is not None
                and op0_shiftamount + op1_v == stmt.dst.bits
            ):
                shiftamount = Const(None, None, op0_shiftamount, 8, **op0.operands[1].tags)
                return Assignment(
                    stmt.idx,
                    stmt.dst,
                    BinaryOp(None, "Rol", [op0.operands[0], shiftamount], False, bits=stmt.dst.bits, **op0.tags),
                    **stmt.tags,
                )
            if (
                op0.op == "Shr"
                and op1.op in {"Shl", "Mul"}
                and (op1_shiftamount := get_expr_shift_left_amount(op1)) is not None
                and op0_v + op1_shiftamount == stmt.dst.bits
            ):
                shiftamount = op0.operands[1]
                return Assignment(
                    stmt.idx,
                    stmt.dst,
                    BinaryOp(None, "Ror", [op0.operands[0], shiftamount], False, bits=stmt.dst.bits, **op0.tags),
                    **stmt.tags,
                )

        return None
