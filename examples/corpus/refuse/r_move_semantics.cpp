// refuse: include-outside-subset/move-semantics
#include <cstdio>
#include <utility>
struct Box { int v; };
int main() {
    Box a{1};
    Box b = std::move(a);
    std::printf("%d\n", b.v);
    return 0;
}
