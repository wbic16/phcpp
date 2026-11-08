// refuse: stl-facility
#include <cstdio>
#include <vector>
int main() {
    std::vector<int> v;
    v.push_back(3);
    std::printf("%d\n", (int)v.size());
    return 0;
}
