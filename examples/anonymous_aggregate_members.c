/* Anonymous aggregate members for a compact tagged point-or-color value. */

enum {
    VALUE_POINT = 1,
    VALUE_COLOR = 2,
};

struct Value {
    int type;
    union {
        struct { int x; int y; };
        struct { int r; int g; };
    };
};

struct Value point(int x, int y) {
    struct Value value = { .type = VALUE_POINT, .x = x, .y = y };
    return value;
}

void translate(struct Value *value, int dx, int dy) {
    value->x += dx;
    value->y += dy;
}

int main(void) {
    struct Value position = point(3, 4);
    struct Value color = { .type = VALUE_COLOR, .r = 6, .g = 7 };

    translate(&position, 2, -1);
    if (position.type != VALUE_POINT || position.x != 5 || position.y != 3)
        return -1;
    if (color.type != VALUE_COLOR || color.x != 6 || color.y != 7)
        return -2;

    return position.x * position.y + color.r + color.g;
}
