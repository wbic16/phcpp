/* slice.c — hand-lowered C for slice.cpp (phcpp v0 vertical slice).
 *
 * Lowered BY HAND for v0 and labelled as such everywhere it is
 * mentioned: no phcpp front end exists yet. The lowering follows the
 * conventions stated in docs/DESIGN.md:
 *   - a polymorphic class becomes a struct whose first member is a
 *     pointer to a vtable struct (function pointers in declaration
 *     order, destructor slot first);
 *   - a constructor becomes Type_init, a destructor Type_destroy,
 *     called explicitly at the point C++ would run it;
 *   - each template instantiation becomes one C function named
 *     <template>__<argument types> (v0 mangling);
 *   - a virtual call s->m(...) becomes s->vptr->m(s, ...).
 */
#include <stdio.h>

/* ---- struct Shape (abstract; lowered from slice.cpp) ---- */
struct Shape_vtable {
    void (*destroy)(void *self);
    double (*area)(const void *self);
    const char *(*name)(const void *self);
};
struct Shape {
    const struct Shape_vtable *vptr;
};

/* ---- struct Circle : Shape ---- */
struct Circle {
    const struct Shape_vtable *vptr;
    double r;
};
static void Circle_destroy(void *self) { (void)self; }
static double Circle_area(const void *self) {
    const struct Circle *c = (const struct Circle *)self;
    return 3.141592653589793 * c->r * c->r;
}
static const char *Circle_name(const void *self) {
    (void)self;
    return "circle";
}
static const struct Shape_vtable Circle_vtable = {
    Circle_destroy, Circle_area, Circle_name
};
static void Circle_init(struct Circle *c, double radius) {
    c->vptr = &Circle_vtable;
    c->r = radius;
}

/* ---- struct Square : Shape ---- */
struct Square {
    const struct Shape_vtable *vptr;
    double s;
};
static void Square_destroy(void *self) { (void)self; }
static double Square_area(const void *self) {
    const struct Square *q = (const struct Square *)self;
    return q->s * q->s;
}
static const char *Square_name(const void *self) {
    (void)self;
    return "square";
}
static const struct Shape_vtable Square_vtable = {
    Square_destroy, Square_area, Square_name
};
static void Square_init(struct Square *q, double side) {
    q->vptr = &Square_vtable;
    q->s = side;
}

/* ---- add<T>: two instantiations ---- */
static int add__int(int a, int b) { return a + b; }
static double add__double(double a, double b) { return a + b; }

/* ---- struct Tracer (destructor has an observable side effect) ---- */
struct Tracer {
    const char *id;
};
static void Tracer_init(struct Tracer *t, const char *name) {
    t->id = name;
    printf("tracer %s constructed\n", t->id);
}
static void Tracer_destroy(struct Tracer *t) {
    printf("tracer %s destroyed\n", t->id);
}

int main(void) {
    struct Circle c;
    struct Square q;
    struct Shape *shapes[2];
    int i;
    Circle_init(&c, 2.0);
    Square_init(&q, 3.0);
    shapes[0] = (struct Shape *)&c;
    shapes[1] = (struct Shape *)&q;
    for (i = 0; i < 2; i++) {
        struct Shape *s = shapes[i];
        printf("%s area %.2f\n", s->vptr->name(s), s->vptr->area(s));
    }
    printf("add int %d\n", add__int(19, 23));
    printf("add double %.1f\n", add__double(1.25, 2.25));
    {
        struct Tracer t;
        Tracer_init(&t, "inner");
        printf("inside scope\n");
        Tracer_destroy(&t);
    }
    printf("after scope\n");
    return 0;
}
