// refuse: coroutine/clang-error
#include <cstdio>
int gen() {
    co_return 1;
}
int main() {
    std::printf("%d\n", gen());
    return 0;
}
