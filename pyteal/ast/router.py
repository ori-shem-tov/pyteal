from dataclasses import dataclass, field, fields, astuple
from typing import cast, Optional, Callable
from enum import IntFlag

from algosdk import abi as sdk_abi
from algosdk import encoding

from pyteal.config import METHOD_ARG_NUM_CUTOFF
from pyteal.errors import TealInputError, TealInternalError
from pyteal.types import TealType
from pyteal.compiler.compiler import compileTeal, DEFAULT_TEAL_VERSION, OptimizeOptions
from pyteal.ir.ops import Mode

from pyteal.ast import abi
from pyteal.ast.subroutine import (
    OutputKwArgInfo,
    SubroutineFnWrapper,
    ABIReturnSubroutine,
)
from pyteal.ast.assert_ import Assert
from pyteal.ast.cond import Cond
from pyteal.ast.expr import Expr
from pyteal.ast.app import OnComplete, EnumInt
from pyteal.ast.int import Int
from pyteal.ast.seq import Seq
from pyteal.ast.methodsig import MethodSignature
from pyteal.ast.naryexpr import And, Or
from pyteal.ast.txn import Txn
from pyteal.ast.return_ import Approve


class CallConfig(IntFlag):
    """
    CallConfig: a "bitset"-like class for more fine-grained control over
    `call or create` for a method about an OnComplete case.

    This enumeration class allows for specifying one of the four following cases:
    - CALL
    - CREATE
    - ALL
    - NEVER
    for a method call on one on_complete case.
    """

    NEVER = 0
    CALL = 1
    CREATE = 2
    ALL = 3

    def condition_under_config(self) -> Expr | int:
        match self:
            case CallConfig.NEVER:
                return 0
            case CallConfig.CALL:
                return Txn.application_id() != Int(0)
            case CallConfig.CREATE:
                return Txn.application_id() == Int(0)
            case CallConfig.ALL:
                return 1
            case _:
                raise TealInternalError(f"unexpected CallConfig {self}")


CallConfig.__module__ = "pyteal"


@dataclass(frozen=True)
class MethodConfig:
    """
    MethodConfig keep track of one method's CallConfigs for all OnComplete cases.

    The `MethodConfig` implementation generalized contract method call such that the registered
    method call is paired with certain OnCompletion conditions and creation conditions.
    """

    no_op: CallConfig = field(kw_only=True, default=CallConfig.CALL)
    opt_in: CallConfig = field(kw_only=True, default=CallConfig.NEVER)
    close_out: CallConfig = field(kw_only=True, default=CallConfig.NEVER)
    clear_state: CallConfig = field(kw_only=True, default=CallConfig.NEVER)
    update_application: CallConfig = field(kw_only=True, default=CallConfig.NEVER)
    delete_application: CallConfig = field(kw_only=True, default=CallConfig.NEVER)

    def is_never(self) -> bool:
        return all(map(lambda cc: cc == CallConfig.NEVER, astuple(self)))

    @classmethod
    def arc4_compliant(cls):
        return cls(
            no_op=CallConfig.ALL,
            opt_in=CallConfig.ALL,
            close_out=CallConfig.ALL,
            clear_state=CallConfig.ALL,
            update_application=CallConfig.ALL,
            delete_application=CallConfig.ALL,
        )

    def is_arc4_compliant(self) -> bool:
        return self == self.arc4_compliant()

    def approval_cond(self) -> Expr | int:
        config_oc_pairs: list[tuple[CallConfig, EnumInt]] = [
            (self.no_op, OnComplete.NoOp),
            (self.opt_in, OnComplete.OptIn),
            (self.close_out, OnComplete.CloseOut),
            (self.update_application, OnComplete.UpdateApplication),
            (self.delete_application, OnComplete.DeleteApplication),
        ]
        if all(config == CallConfig.NEVER for config, _ in config_oc_pairs):
            return 0
        elif all(config == CallConfig.ALL for config, _ in config_oc_pairs):
            return 1
        else:
            cond_list = []
            for config, oc in config_oc_pairs:
                config_cond = config.condition_under_config()
                match config_cond:
                    case Expr():
                        cond_list.append(And(Txn.on_completion() == oc, config_cond))
                    case 1:
                        cond_list.append(Txn.on_completion() == oc)
                    case 0:
                        continue
                    case _:
                        raise TealInternalError(
                            f"unexpected condition_under_config: {config_cond}"
                        )
            return Or(*cond_list)

    def clear_state_cond(self) -> Expr | int:
        return self.clear_state.condition_under_config()


@dataclass(frozen=True)
class OnCompleteAction:
    """
    OnComplete Action, registers bare calls to one single OnCompletion case.
    """

    action: Optional[Expr | SubroutineFnWrapper | ABIReturnSubroutine] = field(
        kw_only=True, default=None
    )
    call_config: CallConfig = field(kw_only=True, default=CallConfig.NEVER)

    def __post_init__(self):
        if bool(self.call_config) ^ bool(self.action):
            raise TealInputError(
                f"action {self.action} and call_config {str(self.call_config)} contradicts"
            )

    @staticmethod
    def never() -> "OnCompleteAction":
        return OnCompleteAction()

    @staticmethod
    def create_only(
        f: Expr | SubroutineFnWrapper | ABIReturnSubroutine,
    ) -> "OnCompleteAction":
        return OnCompleteAction(action=f, call_config=CallConfig.CREATE)

    @staticmethod
    def call_only(
        f: Expr | SubroutineFnWrapper | ABIReturnSubroutine,
    ) -> "OnCompleteAction":
        return OnCompleteAction(action=f, call_config=CallConfig.CALL)

    @staticmethod
    def always(
        f: Expr | SubroutineFnWrapper | ABIReturnSubroutine,
    ) -> "OnCompleteAction":
        return OnCompleteAction(action=f, call_config=CallConfig.ALL)

    def is_empty(self) -> bool:
        return not self.action and self.call_config == CallConfig.NEVER


OnCompleteAction.__module__ = "pyteal"


@dataclass(frozen=True)
class BareCallActions:
    """
    BareCallActions keep track of bare-call registrations to all OnCompletion cases.
    """

    close_out: OnCompleteAction = field(kw_only=True, default=OnCompleteAction.never())
    clear_state: OnCompleteAction = field(
        kw_only=True, default=OnCompleteAction.never()
    )
    delete_application: OnCompleteAction = field(
        kw_only=True, default=OnCompleteAction.never()
    )
    no_op: OnCompleteAction = field(kw_only=True, default=OnCompleteAction.never())
    opt_in: OnCompleteAction = field(kw_only=True, default=OnCompleteAction.never())
    update_application: OnCompleteAction = field(
        kw_only=True, default=OnCompleteAction.never()
    )

    def is_empty(self) -> bool:
        for action_field in fields(self):
            action: OnCompleteAction = getattr(self, action_field.name)
            if not action.is_empty():
                return False
        return True

    def approval_construction(self) -> Optional[Expr]:
        oc_action_pair: list[tuple[EnumInt, OnCompleteAction]] = [
            (OnComplete.NoOp, self.no_op),
            (OnComplete.OptIn, self.opt_in),
            (OnComplete.CloseOut, self.close_out),
            (OnComplete.UpdateApplication, self.update_application),
            (OnComplete.DeleteApplication, self.delete_application),
        ]
        if all(oca.is_empty() for _, oca in oc_action_pair):
            return None
        conditions_n_branches: list[CondNode] = list()
        for oc, oca in oc_action_pair:
            if oca.is_empty():
                continue
            wrapped_handler = ASTBuilder.wrap_handler(
                False,
                cast(Expr | SubroutineFnWrapper | ABIReturnSubroutine, oca.action),
            )
            match oca.call_config:
                case CallConfig.ALL:
                    cond_body = wrapped_handler
                case CallConfig.CALL | CallConfig.CREATE:
                    cond_body = Seq(
                        Assert(cast(Expr, oca.call_config.condition_under_config())),
                        wrapped_handler,
                    )
                case _:
                    raise TealInternalError(
                        f"Unexpected CallConfig: {str(oca.call_config)}"
                    )
            conditions_n_branches.append(
                CondNode(
                    Txn.on_completion() == oc,
                    cond_body,
                )
            )
        return Cond(*[[n.condition, n.branch] for n in conditions_n_branches])

    def clear_state_construction(self) -> Optional[Expr]:
        if self.clear_state.is_empty():
            return None

        wrapped_handler = ASTBuilder.wrap_handler(
            False,
            cast(
                Expr | SubroutineFnWrapper | ABIReturnSubroutine,
                self.clear_state.action,
            ),
        )
        match self.clear_state.call_config:
            case CallConfig.ALL:
                return wrapped_handler
            case CallConfig.CALL | CallConfig.CREATE:
                return Seq(
                    Assert(
                        cast(
                            Expr, self.clear_state.call_config.condition_under_config()
                        )
                    ),
                    wrapped_handler,
                )
            case _:
                raise TealInternalError(
                    f"Unexpected CallConfig: {str(self.clear_state.call_config)}"
                )


BareCallActions.__module__ = "pyteal"


@dataclass(frozen=True)
class CondNode:
    condition: Expr
    branch: Expr


CondNode.__module__ = "pyteal"


@dataclass
class ASTBuilder:
    conditions_n_branches: list[CondNode] = field(default_factory=list)

    @staticmethod
    def wrap_handler(
        is_method_call: bool, handler: ABIReturnSubroutine | SubroutineFnWrapper | Expr
    ) -> Expr:
        """This is a helper function that handles transaction arguments passing in bare-app-call/abi-method handlers.

        If `is_method_call` is True, then it can only be `ABIReturnSubroutine`,
        otherwise:
            - both `ABIReturnSubroutine` and `Subroutine` takes 0 argument on the stack.
            - all three cases have none (or void) type.

        On ABI method case, if the ABI method has more than 15 args, this function manages to de-tuple
        the last (16-th) Txn app-arg into a list of ABI method arguments, and pass in to the ABI method.

        Args:
            is_method_call: a boolean value that specify if the handler is an ABI method.
            handler: an `ABIReturnSubroutine`, or `SubroutineFnWrapper` (for `Subroutine` case), or an `Expr`.
        Returns:
            Expr:
                - for bare-appcall it returns an expression that the handler takes no txn arg and Approve
                - for abi-method it returns the txn args correctly decomposed into ABI variables,
                  passed in ABIReturnSubroutine and logged, then approve.
        """
        if not is_method_call:
            match handler:
                case Expr():
                    if handler.type_of() != TealType.none:
                        raise TealInputError(
                            f"bare appcall handler should be TealType.none not {handler.type_of()}."
                        )
                    return handler if handler.has_return() else Seq(handler, Approve())
                case SubroutineFnWrapper():
                    if handler.type_of() != TealType.none:
                        raise TealInputError(
                            f"subroutine call should be returning TealType.none not {handler.type_of()}."
                        )
                    if handler.subroutine.argument_count() != 0:
                        raise TealInputError(
                            f"subroutine call should take 0 arg for bare-app call. "
                            f"this subroutine takes {handler.subroutine.argument_count()}."
                        )
                    return Seq(handler(), Approve())
                case ABIReturnSubroutine():
                    if handler.type_of() != "void":
                        raise TealInputError(
                            f"abi-returning subroutine call should be returning void not {handler.type_of()}."
                        )
                    if handler.subroutine.argument_count() != 0:
                        raise TealInputError(
                            f"abi-returning subroutine call should take 0 arg for bare-app call. "
                            f"this abi-returning subroutine takes {handler.subroutine.argument_count()}."
                        )
                    return Seq(cast(Expr, handler()), Approve())
                case _:
                    raise TealInputError(
                        "bare appcall can only accept: none type Expr, or Subroutine/ABIReturnSubroutine with none return and no arg"
                    )
        else:
            if not isinstance(handler, ABIReturnSubroutine):
                raise TealInputError(
                    f"method call should be only registering ABIReturnSubroutine, got {type(handler)}."
                )
            if not handler.is_abi_routable():
                raise TealInputError(
                    f"method call ABIReturnSubroutine is not routable "
                    f"got {handler.subroutine.argument_count()} args with {len(handler.subroutine.abi_args)} ABI args."
                )

            # All subroutine args types
            arg_type_specs = cast(
                list[abi.TypeSpec], handler.subroutine.expected_arg_types
            )

            # All subroutine arg values, initialize here and use below instead of
            # creating new instances on the fly so we dont have to think about splicing
            # back in the transaction types
            arg_vals = [typespec.new_instance() for typespec in arg_type_specs]

            # Only args that appear in app args
            app_arg_vals: list[abi.BaseType] = [
                ats for ats in arg_vals if not isinstance(ats, abi.Transaction)
            ]
            tuplify = len(app_arg_vals) > METHOD_ARG_NUM_CUTOFF

            # only transaction args (these are omitted from app args)
            txn_arg_vals: list[abi.BaseType] = [
                ats for ats in arg_vals if isinstance(ats, abi.Transaction)
            ]

            # Tuple-ify any app args after the limit
            if tuplify:
                last_arg_specs_grouped: list[abi.TypeSpec] = [
                    t.type_spec() for t in app_arg_vals[METHOD_ARG_NUM_CUTOFF - 1 :]
                ]
                app_arg_vals = app_arg_vals[: METHOD_ARG_NUM_CUTOFF - 1]
                last_arg_spec = abi.TupleTypeSpec(
                    *last_arg_specs_grouped
                ).new_instance()
                app_arg_vals.append(last_arg_spec)

            # decode app args
            decode_instructions: list[Expr] = [
                app_arg_vals[i].decode(Txn.application_args[i + 1])
                for i in range(len(app_arg_vals))
            ]

            # "decode" transaction types by setting the relative index
            if len(txn_arg_vals) > 0:
                txn_decode_instructions: list[Expr] = []
                txn_relative_pos = len(txn_arg_vals)
                for i in range(len(txn_arg_vals)):
                    txn_decode_instructions.append(
                        cast(abi.Transaction, txn_arg_vals[i]).set(
                            Txn.group_index() - Int(txn_relative_pos - i)
                        ),
                    )

                decode_instructions += txn_decode_instructions

            if tuplify:
                tuple_abi_args: list[abi.BaseType] = arg_vals[
                    METHOD_ARG_NUM_CUTOFF - 1 :
                ]
                last_tuple_arg: abi.Tuple = cast(abi.Tuple, app_arg_vals[-1])
                de_tuple_instructions: list[Expr] = [
                    last_tuple_arg[i].store_into(tuple_abi_args[i])
                    for i in range(len(tuple_abi_args))
                ]
                decode_instructions += de_tuple_instructions

            # NOTE: does not have to have return, can be void method
            if handler.type_of() == "void":
                return Seq(
                    *decode_instructions,
                    cast(Expr, handler(*arg_vals)),
                    Approve(),
                )
            else:
                output_temp: abi.BaseType = cast(
                    OutputKwArgInfo, handler.output_kwarg_info
                ).abi_type.new_instance()
                subroutine_call: abi.ReturnedValue = cast(
                    abi.ReturnedValue, handler(*arg_vals)
                )
                return Seq(
                    *decode_instructions,
                    subroutine_call.store_into(output_temp),
                    abi.MethodReturn(output_temp),
                    Approve(),
                )

    def add_method_to_ast(
        self, method_signature: str, cond: Expr | int, handler: ABIReturnSubroutine
    ) -> None:
        walk_in_cond = Txn.application_args[0] == MethodSignature(method_signature)
        match cond:
            case Expr():
                self.conditions_n_branches.append(
                    CondNode(
                        walk_in_cond,
                        Seq(Assert(cond), self.wrap_handler(True, handler)),
                    )
                )
            case 1:
                self.conditions_n_branches.append(
                    CondNode(walk_in_cond, self.wrap_handler(True, handler))
                )
            case 0:
                return
            case _:
                raise TealInputError("Invalid condition input for add_method_to_ast")

    def program_construction(self) -> Expr:
        if not self.conditions_n_branches:
            raise TealInputError("ABIRouter: Cannot build program with an empty AST")
        return Cond(*[[n.condition, n.branch] for n in self.conditions_n_branches])


class Router:
    """
    Class that help constructs:
    - a *Generalized* ARC-4 app's approval/clear-state programs
    - and a contract JSON object allowing for easily read and call methods in the contract

    *DISCLAIMER*: ABI-Router is still taking shape and is subject to backwards incompatible changes.

    * Based on feedback, the API and usage patterns are likely to change.
    * Expect migration issues.
    """

    def __init__(
        self,
        name: str,
        bare_calls: BareCallActions = None,
    ) -> None:
        """
        Args:
            name: the name of the smart contract, used in the JSON object.
            bare_calls: the bare app call registered for each on_completion.
        """

        self.name: str = name
        self.approval_ast = ASTBuilder()
        self.clear_state_ast = ASTBuilder()

        self.method_sig_to_selector: dict[str, bytes] = dict()
        self.method_selector_to_sig: dict[bytes, str] = dict()

        if bare_calls and not bare_calls.is_empty():
            bare_call_approval = bare_calls.approval_construction()
            if bare_call_approval:
                self.approval_ast.conditions_n_branches.append(
                    CondNode(
                        Txn.application_args.length() == Int(0),
                        cast(Expr, bare_call_approval),
                    )
                )
            bare_call_clear = bare_calls.clear_state_construction()
            if bare_call_clear:
                self.clear_state_ast.conditions_n_branches.append(
                    CondNode(
                        Txn.application_args.length() == Int(0),
                        cast(Expr, bare_call_clear),
                    )
                )

    def add_method_handler(
        self,
        method_call: ABIReturnSubroutine,
        overriding_name: str = None,
        method_config: MethodConfig = MethodConfig(),
    ) -> None:
        if not isinstance(method_call, ABIReturnSubroutine):
            raise TealInputError(
                "for adding method handler, must be ABIReturnSubroutine"
            )
        method_signature = method_call.method_signature(overriding_name)
        if method_config.is_never():
            raise TealInputError(
                f"registered method {method_signature} is never executed"
            )
        method_selector = encoding.checksum(bytes(method_signature, "utf-8"))[:4]

        if method_signature in self.method_sig_to_selector:
            raise TealInputError(f"re-registering method {method_signature} detected")
        if method_selector in self.method_selector_to_sig:
            raise TealInputError(
                f"re-registering method {method_signature} has hash collision "
                f"with {self.method_selector_to_sig[method_selector]}"
            )
        self.method_sig_to_selector[method_signature] = method_selector
        self.method_selector_to_sig[method_selector] = method_signature

        if method_config.is_arc4_compliant():
            self.approval_ast.add_method_to_ast(method_signature, 1, method_call)
            self.clear_state_ast.add_method_to_ast(method_signature, 1, method_call)
            return

        method_approval_cond = method_config.approval_cond()
        method_clear_state_cond = method_config.clear_state_cond()
        self.approval_ast.add_method_to_ast(
            method_signature, method_approval_cond, method_call
        )
        self.clear_state_ast.add_method_to_ast(
            method_signature, method_clear_state_cond, method_call
        )

    def method(
        self,
        func: Callable = None,
        /,
        *,
        name: str = None,
        no_op: CallConfig = CallConfig.CALL,
        opt_in: CallConfig = CallConfig.NEVER,
        close_out: CallConfig = CallConfig.NEVER,
        clear_state: CallConfig = CallConfig.NEVER,
        update_application: CallConfig = CallConfig.NEVER,
        delete_application: CallConfig = CallConfig.NEVER,
    ):
        """
        A decorator style method registration by decorating over a python function,
        which is internally converted to ABIReturnSubroutine, and taking keyword arguments
        for each OnCompletes' `CallConfig`.

        NOTE:
            By default, all OnCompletes other than `NoOp` are set to `CallConfig.NEVER`,
            while `no_op` field is always `CALL`.
            If one wants to change `no_op`,  we need to change `no_op = CallConfig.ALL`,
            for example, as a decorator argument.
        """

        def wrap(_func):
            wrapped_subroutine = ABIReturnSubroutine(_func)
            call_configs = MethodConfig(
                no_op=no_op,
                opt_in=opt_in,
                close_out=close_out,
                clear_state=clear_state,
                update_application=update_application,
                delete_application=delete_application,
            )
            self.add_method_handler(wrapped_subroutine, name, call_configs)

        if not func:
            return wrap
        return wrap(func)

    def contract_construct(self) -> sdk_abi.Contract:
        """A helper function in constructing contract JSON object.

        It takes out the method signatures from approval program `ProgramNode`'s,
        and constructs an `Contract` object.

        Returns:
            contract: a dictified `Contract` object constructed from
                approval program's method signatures and `self.name`.
        """
        method_collections = [
            sdk_abi.Method.from_signature(sig)
            for sig in self.method_sig_to_selector
            if isinstance(sig, str)
        ]
        return sdk_abi.Contract(self.name, method_collections)

    def build_program(self) -> tuple[Expr, Expr, sdk_abi.Contract]:
        """
        Constructs ASTs for approval and clear-state programs from the registered methods in the router,
        also generates a JSON object of contract to allow client read and call the methods easily.

        Returns:
            approval_program: AST for approval program
            clear_state_program: AST for clear-state program
            contract: JSON object of contract to allow client start off-chain call
        """
        return (
            self.approval_ast.program_construction(),
            self.clear_state_ast.program_construction(),
            self.contract_construct(),
        )

    def compile_program(
        self,
        *,
        version: int = DEFAULT_TEAL_VERSION,
        assemble_constants: bool = False,
        optimize: OptimizeOptions = None,
    ) -> tuple[str, str, sdk_abi.Contract]:
        """
        Combining `build_program` and `compileTeal`, compiles built Approval and ClearState programs
        and returns Contract JSON object for off-chain calling.

        Returns:
            approval_program: compiled approval program
            clear_state_program: compiled clear-state program
            contract: JSON object of contract to allow client start off-chain call
        """
        ap, csp, contract = self.build_program()
        ap_compiled = compileTeal(
            ap,
            Mode.Application,
            version=version,
            assembleConstants=assemble_constants,
            optimize=optimize,
        )
        csp_compiled = compileTeal(
            csp,
            Mode.Application,
            version=version,
            assembleConstants=assemble_constants,
            optimize=optimize,
        )
        return ap_compiled, csp_compiled, contract


Router.__module__ = "pyteal"
