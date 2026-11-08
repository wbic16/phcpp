// refuse: exceptions
#include <cstdio>
int main() {
    try {
        std::printf("in try\n");
    } catch (...) {
        std::printf("caught\n");
    }
    return 0;
}
