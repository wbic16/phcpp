// 07_heap.cpp — new/delete of single objects: constructor arguments
// forwarded through the per-class helpers, and a virtual destructor
// dispatching through a base pointer on delete.
#include <cstdio>

struct Node {
    int v;
    Node(int x) : v(x) {}
    virtual ~Node() { std::printf("~Node %d\n", v); }
    virtual int val() const { return v; }
};

struct Leaf : Node {
    Leaf(int x) : Node(x) {}
    ~Leaf() { std::printf("~Leaf %d\n", v); }
    int val() const override { return v * 10; }
};

struct Plain {
    int v;
    Plain() : v(1) {}
    ~Plain() { std::printf("~Plain\n"); }
};

int main() {
    Node* a = new Leaf(4);
    std::printf("val=%d\n", a->val());
    delete a;
    Node* b = new Node(2);
    std::printf("val=%d\n", b->val());
    delete b;
    Plain* p = new Plain;
    std::printf("plain=%d\n", p->v);
    delete p;
    return 0;
}
