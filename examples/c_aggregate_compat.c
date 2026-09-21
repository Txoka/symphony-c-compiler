/* Exercises goto, switch, anonymous aggregates, designated/brace-elided initializers, unions, and copies. */
#include <stdio.h>

struct Pair {
    int left;
    int right;
};

union Word {
    int value;
    unsigned char bytes[4];
};

struct Value {
    int type;
    union {
        struct { int x; int y; };
        struct { int r; int g; };
    };
};

struct Pair make_pair(int left, int right) {
    struct Pair pair = { left, right };
    return pair;
}

int main(void) {
    struct Pair original = make_pair(10, 20);
    struct Pair selected = { .right = 7, .left = 4 };
    int lookup[5] = { [3] = 9, [1] = 2 };
    union Word selector = { .bytes = { 0, 0, 0, 3 } };
    struct Value value = { .type = 1, .x = 4, .y = 7 };

    selected = original;
    if (value.x != 4 || value.y != 7) return -3;
    value.r = 10;
    value.g = 20;
    if (value.x != 10 || value.y != 20) return -4;
    goto dispatch;

unexpected:
    return -2;

dispatch:
    switch (selector.value) {
    case 1:
        return 0;
    case 3:
        printf("aggregate compatibility: %d", selected.left + selected.right);
        return selected.left + selected.right + lookup[1] + lookup[3];
    default:
        return -1;
    }
}
