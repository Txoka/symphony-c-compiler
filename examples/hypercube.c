#include <symphony.h>

/* ========================================================================= */
/* Configuration                                                             */
/* ========================================================================= */

#define SCREEN_SETTING 255u

#define WIDTH       (4u * (SCREEN_SETTING + 1u))
#define HEIGHT      (3u * (SCREEN_SETTING + 1u))
#define PIXEL_COUNT (WIDTH * HEIGHT)

#define BLACK_PIXEL 0x00000000u

/* Toggle this */
#define SHOW_POINTS 0


/* ------------------------------------------------------------------------- */
/* Animation                                                                 */
/* ------------------------------------------------------------------------- */

#define ROTATION_STEPS   512u
#define ROTATION_MASK    (ROTATION_STEPS - 1u)
#define ROTATION_QUARTER (ROTATION_STEPS / 4u)

#define STEP_NS 31250000u


/* ------------------------------------------------------------------------- */
/* Fixed point                                                               */
/* ------------------------------------------------------------------------- */

#define MODEL_SHIFT 10
#define MODEL_ONE   (1 << MODEL_SHIFT)

#define TRIG_SHIFT 15


/* ------------------------------------------------------------------------- */
/* Tesseract                                                                 */
/* ------------------------------------------------------------------------- */

#define HALF_SIDE (10 * MODEL_ONE)

#define CENTER_Z (15 * MODEL_ONE)
#define CENTER_W (15 * MODEL_ONE)

#define UV_TO_PIXEL_DENOM (20 * MODEL_ONE)


/* ------------------------------------------------------------------------- */
/* Depth shading                                                             */
/* ------------------------------------------------------------------------- */

#define EDGE_NEAR_BRIGHTNESS 255u
#define EDGE_FAR_BRIGHTNESS   70u

#if SHOW_POINTS

#define POINT_DEPTH_LEVELS 16u

#define POINT_FAR_SCALE  128u
#define POINT_NEAR_SCALE 255u

/*
 * Much tighter cutoff than the previous slow version.
 */
#define POINT_MIN_INTENSITY 48u
#define POINT_RADIUS 16

#define POINT_DIAMETER \
    (2 * POINT_RADIUS + 1)

#define POINT_BRUSH_MAX \
    (POINT_DIAMETER * POINT_DIAMETER)

#endif


/* ------------------------------------------------------------------------- */
/* Dirty-list capacity                                                       */
/* ------------------------------------------------------------------------- */

#if SHOW_POINTS

#define DIRTY_CAPACITY \
    (96u * WIDTH + 16u * POINT_BRUSH_MAX + 512u)

#else

#define DIRTY_CAPACITY \
    (96u * WIDTH + 512u)

#endif


/* ========================================================================= */
/* Types                                                                     */
/* ========================================================================= */

struct Vec4 {
    int x;
    int y;
    int z;
    int w;
};


/* ========================================================================= */
/* Framebuffers                                                              */
/* ========================================================================= */

static unsigned int framebuffer[2][PIXEL_COUNT];

static unsigned int dirty_pixels[2][DIRTY_CAPACITY];
static unsigned int dirty_count[2];


/* ========================================================================= */
/* Precomputed animation                                                     */
/* ========================================================================= */

static short projected_x[ROTATION_STEPS][16];
static short projected_y[ROTATION_STEPS][16];

static int vertex_depth[ROTATION_STEPS][16];

static unsigned char edge_order[ROTATION_STEPS][32];
static unsigned int edge_color[ROTATION_STEPS][32];

#if SHOW_POINTS

static unsigned char vertex_depth_level[ROTATION_STEPS][16];

#endif


/* ========================================================================= */
/* Tesseract topology                                                        */
/* ========================================================================= */

static unsigned char edges[32][2] = {
    {  0,  1 },
    {  0,  2 },
    {  0,  4 },
    {  0,  8 },

    {  1,  3 },
    {  1,  5 },
    {  1,  9 },

    {  2,  3 },
    {  2,  6 },
    {  2, 10 },

    {  3,  7 },
    {  3, 11 },

    {  4,  5 },
    {  4,  6 },
    {  4, 12 },

    {  5,  7 },
    {  5, 13 },

    {  6,  7 },
    {  6, 14 },

    {  7, 15 },

    {  8,  9 },
    {  8, 10 },
    {  8, 12 },

    {  9, 11 },
    {  9, 13 },

    { 10, 11 },
    { 10, 14 },

    { 11, 15 },

    { 12, 13 },
    { 12, 14 },

    { 13, 15 },

    { 14, 15 }
};


/* ========================================================================= */
/* Optional point glow                                                       */
/* ========================================================================= */

#if SHOW_POINTS

static int point_offset[POINT_BRUSH_MAX];
static unsigned char point_intensity[POINT_BRUSH_MAX];

static unsigned int point_brush_count;

/*
 * Prepacked Pixel32 colors:
 *
 * [depth level][base glow intensity]
 */
static unsigned int point_color[
    POINT_DEPTH_LEVELS
][256];

#endif


/* ========================================================================= */
/* Quarter-wave sine LUT                                                     */
/* ========================================================================= */

static short sine_quarter[129] = {
       0,   402,   804,  1206,  1608,  2009,  2411,  2811,
    3212,  3612,  4011,  4410,  4808,  5205,  5602,  5998,
    6393,  6787,  7180,  7571,  7962,  8351,  8740,  9127,
    9512,  9896, 10279, 10660, 11039, 11417, 11793, 12167,
   12540, 12910, 13279, 13646, 14010, 14373, 14733, 15091,
   15447, 15800, 16151, 16500, 16846, 17190, 17531, 17869,
   18205, 18538, 18868, 19195, 19520, 19841, 20160, 20475,
   20788, 21097, 21403, 21706, 22006, 22302, 22595, 22884,
   23170, 23453, 23732, 24008, 24279, 24548, 24812, 25073,
   25330, 25583, 25833, 26078, 26320, 26557, 26791, 27020,
   27246, 27467, 27684, 27897, 28106, 28311, 28511, 28707,
   28899, 29086, 29269, 29448, 29622, 29792, 29957, 30118,
   30274, 30425, 30572, 30715, 30853, 30986, 31114, 31238,
   31357, 31471, 31581, 31686, 31786, 31881, 31972, 32058,
   32138, 32214, 32286, 32352, 32413, 32470, 32522, 32568,
   32610, 32647, 32679, 32706, 32729, 32746, 32758, 32766,
   32767
};


static int sin512(unsigned int step)
{
    unsigned int quadrant;
    unsigned int offset;

    step &= ROTATION_MASK;

    quadrant = step >> 7;
    offset = step & 127u;

    if (quadrant == 0u)
        return (int)sine_quarter[offset];

    if (quadrant == 1u)
        return (int)sine_quarter[128u - offset];

    if (quadrant == 2u)
        return -(int)sine_quarter[offset];

    return -(int)sine_quarter[128u - offset];
}


static int cos512(unsigned int step)
{
    return sin512(step + ROTATION_QUARTER);
}


/* ========================================================================= */
/* Helpers                                                                   */
/* ========================================================================= */

static int iabs(int value)
{
    if (value < 0)
        return -value;

    return value;
}


static unsigned int pack_gray(unsigned int value)
{
    return
          (value << 24)
        | (value << 16)
        | (value << 8);
}


/* ========================================================================= */
/* 4D rotation                                                               */
/* ========================================================================= */

static void rotate_pair(
    int *a,
    int *b,
    int c,
    int s
)
{
    int old_a;
    int old_b;

    old_a = *a;
    old_b = *b;

    *a =
        (old_a * c + old_b * s)
        >> TRIG_SHIFT;

    *b =
        (-old_a * s + old_b * c)
        >> TRIG_SHIFT;
}


static void rotate_vertex(
    struct Vec4 *p,
    int c,
    int s
)
{
    rotate_pair(&p->x, &p->y, c, s);
    rotate_pair(&p->x, &p->z, c, s);
    rotate_pair(&p->x, &p->w, c, s);

    rotate_pair(&p->y, &p->z, c, s);
    rotate_pair(&p->y, &p->w, c, s);

    rotate_pair(&p->z, &p->w, c, s);
}


/* ========================================================================= */
/* Projection                                                                */
/* ========================================================================= */

static void project_vertex(
    const struct Vec4 *p,
    short *screen_x,
    short *screen_y,
    int *depth
)
{
    int denominator;

    int px_q10;
    int py_q10;

    int px;
    int py;

    denominator =
          p->z
        + 3 * p->w
        + 60 * MODEL_ONE;

    *depth =
        denominator;

    px_q10 =
        (30 * p->x * MODEL_ONE)
        / denominator;

    py_q10 =
        (30 * p->y * MODEL_ONE)
        / denominator;

    px =
          (int)(WIDTH / 2u)
        + (
            px_q10
            * (int)HEIGHT
          )
          / UV_TO_PIXEL_DENOM;

    py =
          (int)(HEIGHT / 2u)
        - (
            py_q10
            * (int)HEIGHT
          )
          / UV_TO_PIXEL_DENOM;

    *screen_x =
        (short)px;

    *screen_y =
        (short)py;
}


/* ========================================================================= */
/* Precompute projected frames                                               */
/* ========================================================================= */

static void initialize_projected_frames(void)
{
    unsigned int step;
    unsigned int vertex;

    int c;
    int s;

    struct Vec4 p;

    step = 0u;

    while (step < ROTATION_STEPS) {
        c = cos512(step);
        s = sin512(step);

        vertex = 0u;

        while (vertex < 16u) {
            p.x =
                (vertex & 1u)
                ? HALF_SIDE
                : -HALF_SIDE;

            p.y =
                (vertex & 2u)
                ? HALF_SIDE
                : -HALF_SIDE;

            p.z =
                (vertex & 4u)
                ? HALF_SIDE
                : -HALF_SIDE;

            p.w =
                (vertex & 8u)
                ? HALF_SIDE
                : -HALF_SIDE;

            rotate_vertex(
                &p,
                c,
                s
            );

            p.z += CENTER_Z;
            p.w += CENTER_W;

            project_vertex(
                &p,

                &projected_x[step][vertex],
                &projected_y[step][vertex],

                &vertex_depth[step][vertex]
            );

            vertex += 1u;
        }

        step += 1u;
    }
}


/* ========================================================================= */
/* Depth preprocessing                                                       */
/* ========================================================================= */

static void initialize_depth_shading(void)
{
    unsigned int step;
    unsigned int vertex;
    unsigned int edge;

    unsigned int i;
    unsigned int j;

    unsigned int tmp_order;

    int minimum_depth;
    int maximum_depth;
    int depth_range;

    int depth;

    int edge_depth[32];

    unsigned int brightness;

#if SHOW_POINTS
    unsigned int level;
#endif


    minimum_depth =
        vertex_depth[0][0];

    maximum_depth =
        vertex_depth[0][0];


    /* Find global depth range. */

    step = 0u;

    while (step < ROTATION_STEPS) {
        vertex = 0u;

        while (vertex < 16u) {
            depth =
                vertex_depth[step][vertex];

            if (depth < minimum_depth)
                minimum_depth = depth;

            if (depth > maximum_depth)
                maximum_depth = depth;

            vertex += 1u;
        }

        step += 1u;
    }


    depth_range =
        maximum_depth - minimum_depth;

    if (depth_range <= 0)
        depth_range = 1;


    /* Build per-frame data. */

    step = 0u;

    while (step < ROTATION_STEPS) {

#if SHOW_POINTS

        vertex = 0u;

        while (vertex < 16u) {
            depth =
                vertex_depth[step][vertex];

            level =
                (unsigned int)(
                    (
                        (maximum_depth - depth)
                        * (int)(POINT_DEPTH_LEVELS - 1u)
                    )
                    / depth_range
                );

            if (level >= POINT_DEPTH_LEVELS)
                level = POINT_DEPTH_LEVELS - 1u;

            vertex_depth_level[step][vertex] =
                (unsigned char)level;

            vertex += 1u;
        }

#endif


        edge = 0u;

        while (edge < 32u) {
            unsigned int a;
            unsigned int b;

            a =
                (unsigned int)edges[edge][0];

            b =
                (unsigned int)edges[edge][1];

            depth =
                (
                    vertex_depth[step][a]
                    + vertex_depth[step][b]
                ) >> 1;

            edge_depth[edge] =
                depth;

            brightness =
                EDGE_FAR_BRIGHTNESS
                +
                (unsigned int)(
                    (
                        (maximum_depth - depth)
                        *
                        (int)(
                            EDGE_NEAR_BRIGHTNESS
                            - EDGE_FAR_BRIGHTNESS
                        )
                    )
                    / depth_range
                );

            if (brightness > 255u)
                brightness = 255u;

            edge_color[step][edge] =
                pack_gray(brightness);

            edge_order[step][edge] =
                (unsigned char)edge;

            edge += 1u;
        }


        /*
         * Insertion sort:
         *
         * far -> near
         */
        i = 1u;

        while (i < 32u) {
            tmp_order =
                (unsigned int)edge_order[step][i];

            j =
                i;

            while (
                j > 0u
                &&
                edge_depth[
                    (unsigned int)edge_order[step][j - 1u]
                ]
                <
                edge_depth[tmp_order]
            ) {
                edge_order[step][j] =
                    edge_order[step][j - 1u];

                j -= 1u;
            }

            edge_order[step][j] =
                (unsigned char)tmp_order;

            i += 1u;
        }


        step += 1u;
    }
}


/* ========================================================================= */
/* Optional point preprocessing                                              */
/* ========================================================================= */

#if SHOW_POINTS

static void initialize_point_brush(void)
{
    int x;
    int y;

    unsigned int d2;

    unsigned int intensity;

    unsigned int numerator;
    unsigned int denominator;

    unsigned int level;
    unsigned int source;
    unsigned int scale;
    unsigned int value;


    point_brush_count =
        0u;


    /*
     * Original shader:
     *
     *     0.03 / distance²
     *
     * mapped into 0...255 screen intensity.
     */
    numerator =
          255u
        * 3u
        * HEIGHT
        * HEIGHT;


    y =
        -POINT_RADIUS;

    while (y <= POINT_RADIUS) {
        x =
            -POINT_RADIUS;

        while (x <= POINT_RADIUS) {
            d2 =
                (unsigned int)(
                    x * x
                    + y * y
                );

            if (d2 == 0u) {
                intensity =
                    255u;
            }
            else if (
                d2
                <=
                (unsigned int)(
                    POINT_RADIUS
                    * POINT_RADIUS
                )
            ) {
                denominator =
                    40000u * d2;

                intensity =
                    numerator / denominator;

                if (intensity > 255u)
                    intensity = 255u;
            }
            else {
                intensity =
                    0u;
            }


            if (intensity >= POINT_MIN_INTENSITY) {
                point_offset[
                    point_brush_count
                ] =
                      y * (int)WIDTH
                    + x;

                point_intensity[
                    point_brush_count
                ] =
                    (unsigned char)intensity;

                point_brush_count +=
                    1u;
            }

            x += 1;
        }

        y += 1;
    }


    /*
     * Prepack depth-scaled point colors.
     *
     * Runtime point rendering therefore does no multiplication,
     * packing or blending.
     */
    level =
        0u;

    while (level < POINT_DEPTH_LEVELS) {
        scale =
            POINT_FAR_SCALE
            +
            (
                level
                *
                (
                    POINT_NEAR_SCALE
                    - POINT_FAR_SCALE
                )
            )
            /
            (POINT_DEPTH_LEVELS - 1u);

        source =
            0u;

        while (source < 256u) {
            value =
                (source * scale) >> 8;

            point_color[level][source] =
                pack_gray(value);

            source += 1u;
        }

        level += 1u;
    }
}

#endif


/* ========================================================================= */
/* Line clipping                                                             */
/* ========================================================================= */

#define CLIP_LEFT   1
#define CLIP_RIGHT  ((int)WIDTH - 2)
#define CLIP_TOP    1
#define CLIP_BOTTOM ((int)HEIGHT - 2)

#define CODE_LEFT   1u
#define CODE_RIGHT  2u
#define CODE_TOP    4u
#define CODE_BOTTOM 8u


static unsigned int clip_code(
    int x,
    int y
)
{
    unsigned int code;

    code =
        0u;

    if (x < CLIP_LEFT)
        code |= CODE_LEFT;
    else if (x > CLIP_RIGHT)
        code |= CODE_RIGHT;

    if (y < CLIP_TOP)
        code |= CODE_TOP;
    else if (y > CLIP_BOTTOM)
        code |= CODE_BOTTOM;

    return code;
}


static int clip_line(
    int *x0,
    int *y0,
    int *x1,
    int *y1
)
{
    unsigned int code0;
    unsigned int code1;
    unsigned int code;

    int x;
    int y;

    code0 =
        clip_code(*x0, *y0);

    code1 =
        clip_code(*x1, *y1);


    while (1) {
        if ((code0 | code1) == 0u)
            return 1;

        if ((code0 & code1) != 0u)
            return 0;


        if (code0 != 0u)
            code = code0;
        else
            code = code1;


        if (code & CODE_TOP) {
            if (*y1 == *y0)
                return 0;

            x =
                  *x0
                + (
                    (*x1 - *x0)
                    * (CLIP_TOP - *y0)
                  )
                  / (*y1 - *y0);

            y =
                CLIP_TOP;
        }
        else if (code & CODE_BOTTOM) {
            if (*y1 == *y0)
                return 0;

            x =
                  *x0
                + (
                    (*x1 - *x0)
                    * (CLIP_BOTTOM - *y0)
                  )
                  / (*y1 - *y0);

            y =
                CLIP_BOTTOM;
        }
        else if (code & CODE_RIGHT) {
            if (*x1 == *x0)
                return 0;

            y =
                  *y0
                + (
                    (*y1 - *y0)
                    * (CLIP_RIGHT - *x0)
                  )
                  / (*x1 - *x0);

            x =
                CLIP_RIGHT;
        }
        else {
            if (*x1 == *x0)
                return 0;

            y =
                  *y0
                + (
                    (*y1 - *y0)
                    * (CLIP_LEFT - *x0)
                  )
                  / (*x1 - *x0);

            x =
                CLIP_LEFT;
        }


        if (code == code0) {
            *x0 = x;
            *y0 = y;

            code0 =
                clip_code(x, y);
        }
        else {
            *x1 = x;
            *y1 = y;

            code1 =
                clip_code(x, y);
        }
    }
}


/* ========================================================================= */
/* Fast line renderer                                                        */
/* ========================================================================= */

static unsigned int draw_line(
    unsigned int *buffer,
    unsigned int *dirty,
    unsigned int count,

    int x0,
    int y0,
    int x1,
    int y1,

    unsigned int color
)
{
    int dx;
    int abs_dy;
    int dy;

    int sx;
    int sy;

    int error;
    int e2;

    int offset;
    int vertical_step;

    int mostly_horizontal;

    unsigned int a;
    unsigned int b;
    unsigned int c;


    if (!clip_line(
            &x0,
            &y0,
            &x1,
            &y1
        )) {
        return count;
    }


    dx =
        iabs(x1 - x0);

    abs_dy =
        iabs(y1 - y0);

    sx =
        (x0 < x1)
        ? 1
        : -1;

    sy =
        (y0 < y1)
        ? 1
        : -1;

    dy =
        -abs_dy;

    error =
        dx + dy;


    offset =
          y0 * (int)WIDTH
        + x0;

    vertical_step =
        sy * (int)WIDTH;

    mostly_horizontal =
        dx >= abs_dy;


    while (1) {
        if (mostly_horizontal) {
            a =
                (unsigned int)(
                    offset - (int)WIDTH
                );

            b =
                (unsigned int)offset;

            c =
                (unsigned int)(
                    offset + (int)WIDTH
                );
        }
        else {
            a =
                (unsigned int)(
                    offset - 1
                );

            b =
                (unsigned int)offset;

            c =
                (unsigned int)(
                    offset + 1
                );
        }


        buffer[a] =
            color;

        buffer[b] =
            color;

        buffer[c] =
            color;


        dirty[count] =
            a;

        dirty[count + 1u] =
            b;

        dirty[count + 2u] =
            c;

        count +=
            3u;


        if (x0 == x1 && y0 == y1)
            break;


        e2 =
            error << 1;


        if (e2 >= dy) {
            error +=
                dy;

            x0 +=
                sx;

            offset +=
                sx;
        }


        if (e2 <= dx) {
            error +=
                dx;

            y0 +=
                sy;

            offset +=
                vertical_step;
        }
    }


    return count;
}


/* ========================================================================= */
/* Optional point renderer                                                   */
/* ========================================================================= */

#if SHOW_POINTS

static unsigned int draw_point(
    unsigned int *buffer,
    unsigned int *dirty,
    unsigned int count,

    int cx,
    int cy,

    unsigned int depth_level
)
{
    unsigned int i;

    int center;
    int address;

    unsigned int intensity;


    /*
     * Entire point brush must fit.
     */
    if (cx < POINT_RADIUS)
        return count;

    if (cy < POINT_RADIUS)
        return count;

    if (cx >= (int)WIDTH - POINT_RADIUS)
        return count;

    if (cy >= (int)HEIGHT - POINT_RADIUS)
        return count;


    center =
          cy * (int)WIDTH
        + cx;


    i =
        0u;

    while (i < point_brush_count) {
        address =
              center
            + point_offset[i];

        intensity =
            (unsigned int)
            point_intensity[i];

        /*
         * Overwrite rather than additive blend.
         *
         * This is substantially cheaper.
         */
        buffer[address] =
            point_color[
                depth_level
            ][
                intensity
            ];

        dirty[count] =
            (unsigned int)address;

        count +=
            1u;

        i +=
            1u;
    }


    return count;
}

#endif


/* ========================================================================= */
/* Dirty clearing                                                            */
/* ========================================================================= */

static void clear_dirty(
    unsigned int *buffer,
    unsigned int *dirty,
    unsigned int count
)
{
    unsigned int i;

    i =
        0u;

    while (i < count) {
        buffer[
            dirty[i]
        ] =
            BLACK_PIXEL;

        i +=
            1u;
    }
}


/* ========================================================================= */
/* Draw one frame                                                            */
/* ========================================================================= */

static unsigned int draw_tesseract(
    unsigned int *buffer,
    unsigned int *dirty,
    unsigned int step
)
{
    unsigned int count;

    unsigned int order;
    unsigned int edge;

    unsigned int a;
    unsigned int b;

#if SHOW_POINTS
    unsigned int vertex;
#endif


    count =
        0u;


    /* --------------------------------------------------------------------- */
    /* Edges: far -> near                                                    */
    /* --------------------------------------------------------------------- */

    order =
        0u;

    while (order < 32u) {
        edge =
            (unsigned int)
            edge_order[step][order];

        a =
            (unsigned int)
            edges[edge][0];

        b =
            (unsigned int)
            edges[edge][1];


        count =
            draw_line(
                buffer,
                dirty,
                count,

                (int)projected_x[step][a],
                (int)projected_y[step][a],

                (int)projected_x[step][b],
                (int)projected_y[step][b],

                edge_color[step][edge]
            );


        order +=
            1u;
    }


#if SHOW_POINTS

    /* --------------------------------------------------------------------- */
    /* Points                                                                */
    /* --------------------------------------------------------------------- */

    vertex =
        0u;

    while (vertex < 16u) {
        count =
            draw_point(
                buffer,
                dirty,
                count,

                (int)projected_x[step][vertex],
                (int)projected_y[step][vertex],

                (unsigned int)
                vertex_depth_level[step][vertex]
            );

        vertex +=
            1u;
    }

#endif


    return count;
}


/* ========================================================================= */
/* Animation timing                                                          */
/* ========================================================================= */

static unsigned int current_step;
static unsigned int last_step_time;


static int update_animation(void)
{
    unsigned int now;
    unsigned int elapsed;

    int changed;


    now =
        time_low();

    elapsed =
        now - last_step_time;

    changed =
        0;


    while (elapsed >= STEP_NS) {
        last_step_time +=
            STEP_NS;

        elapsed -=
            STEP_NS;

        current_step =
            (current_step + 1u)
            & ROTATION_MASK;

        changed =
            1;
    }


    return changed;
}


/* ========================================================================= */
/* Main                                                                      */
/* ========================================================================= */

int main(void)
{
    unsigned int back_buffer;


    /*
     * Static framebuffer memory starts black.
     */
    screen(
        1u,
        (unsigned int)framebuffer[0]
    );

    screen(
        2u,
        SCREEN_SETTING
    );

    screen(
        0u,
        3u
    );


    /* --------------------------------------------------------------------- */
    /* Startup preprocessing                                                 */
    /* --------------------------------------------------------------------- */

    initialize_projected_frames();

    initialize_depth_shading();

#if SHOW_POINTS
    initialize_point_brush();
#endif


    /* --------------------------------------------------------------------- */
    /* Animation setup                                                       */
    /* --------------------------------------------------------------------- */

    current_step =
        0u;

    last_step_time =
        time_low();

    dirty_count[0] =
        0u;

    dirty_count[1] =
        0u;


    /*
     * framebuffer[0] is currently front.
     */
    back_buffer =
        1u;


    /* --------------------------------------------------------------------- */
    /* First frame                                                           */
    /* --------------------------------------------------------------------- */

    dirty_count[back_buffer] =
        draw_tesseract(
            framebuffer[back_buffer],
            dirty_pixels[back_buffer],
            current_step
        );


    screen(
        1u,
        (unsigned int)framebuffer[back_buffer]
    );


    back_buffer ^=
        1u;


    /* --------------------------------------------------------------------- */
    /* Runtime                                                               */
    /* --------------------------------------------------------------------- */

    while (1) {
        if (update_animation()) {

            clear_dirty(
                framebuffer[back_buffer],
                dirty_pixels[back_buffer],
                dirty_count[back_buffer]
            );


            dirty_count[back_buffer] =
                draw_tesseract(
                    framebuffer[back_buffer],
                    dirty_pixels[back_buffer],
                    current_step
                );


            screen(
                1u,
                (unsigned int)framebuffer[back_buffer]
            );


            back_buffer ^=
                1u;
        }
    }


    return 0;
}