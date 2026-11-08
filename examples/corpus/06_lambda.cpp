// 06_lambda.cpp — non-capturing lambdas: bound to an auto variable,
// stored through a function-pointer typedef, and passed directly as
// a call argument (the closureless lambda -> function pointer
// conversion).
#include <cstdio>

typedef int (*IntFn)(int);

int apply(IntFn f, int x) { return f(x); }

int main() {
    auto sq = [](int x) -> int { return x * x; };
    IntFn inc = [](int x) -> int { return x + 1; };
    std::printf("%d %d %d %d\n",
                sq(9), apply(inc, 41), apply(sq, 5),
                apply([](int x) -> int { return x * 3; }, 7));
    return 0;
}
