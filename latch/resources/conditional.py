from flytekit.core.condition import conditional, ConditionalSection


def if_else_block(name: str) -> ConditionalSection:
    """
    This method creates a new condtional section in a workflow, allowing a user
    to conditionally execute a task based on the value of a task result.

    Conditional sections are akin to ternary operators -- they returns the output
    of the branch result. However, they can be n-ary with as many elif_ clauses
    as desired.

    It is possible to consume the outputs from conditional nodes. And to pass in
    outputs from other tasks to conditional nodes.

    The boolean expressions in the condition use `&` and `|` as and / or operators.
    Additionally, unary expressions are not allowed. Thus if a task returns a boolean
    and we wish to use it in a condition of a conditional block, we must
    use built in truth checks: `result.is_true()` or `result.is_false()`

    Args:
        name: The name of the conditional section, to be shown in Latch Console

    Returns:
        A conditional section

    Intended Use: ::

        @workflow
        def multiplier(my_input: float, file: LatchFile) -> float:
            result_1 = double(n=my_input, file=file)
            result_2 =  (
                if_else_block("fractions")
                .if_((res < 0.0)).then(double(n=res, file=file))
                .elif_((res > 0.0)).then(square(file=file))
                .else_().fail("Only nonzero values allowed")
            )
            result_3 = double(n=res2, file=file)
            return result_3
    """
    return conditional(name)
