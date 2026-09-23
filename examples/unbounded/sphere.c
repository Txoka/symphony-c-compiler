#include <symphony.h>

/*
 * Optimized fixed-point sphere renderer for Dynphony/Symphony.
 *
 * This is the latest version we had converged on:
 *   - 260x195 Pixel32 (SCREEN_SETTING = 64)
 *   - Q10 geometry
 *   - static sphere/camera geometry cached at startup
 *   - signed 8-bit cached normals
 *   - scanline-only framebuffer writes
 *   - 256-step orbiting light
 *   - contribution LUTs, so the per-pixel render loop does
 *     no multiply, divide or square root
 *   - double buffering
 */

/* ========================================================================= */
/* Screen                                                                    */
/* ========================================================================= */

#define SCREEN_SETTING 64u

#define WIDTH       (4u * (SCREEN_SETTING + 1u))
#define HEIGHT      (3u * (SCREEN_SETTING + 1u))
#define PIXEL_COUNT (WIDTH * HEIGHT)

#define BLACK_PIXEL 0x00000000u


/* ========================================================================= */
/* Fixed-point geometry                                                      */
/* ========================================================================= */

#define GEOM_SHIFT 10
#define GEOM_ONE   (1 << GEOM_SHIFT)

/*
 * Sphere:
 *
 *     center = (0, 0, 0)
 *     radius = 1
 *
 * Camera:
 *
 *     (0, 0, -3)
 *
 * Image plane:
 *
 *     z = -1
 *
 * A ray therefore has direction:
 *
 *     D = (u, v, 2)
 */
#define SPHERE_RADIUS GEOM_ONE

#define CAMERA_Z (-3 * GEOM_ONE)

#define RAY_Z (2 * GEOM_ONE)

/*
 * Image-plane half extents.
 *
 * 1376 / 1024 = 1.34375, close to 4:3.
 */
#define PLANE_HALF_X 1376
#define PLANE_HALF_Y 1024


/* ========================================================================= */
/* Cached-normal format                                                      */
/* ========================================================================= */

/*
 * Cached normals are Q7 signed chars:
 *
 *     -128 .. +127 approximately represents -1 .. +1
 *
 * Q10 hit normals are shifted right by three bits.
 */
#define NORMAL_SHIFT 7
#define NORMAL_BIAS  128


/* ========================================================================= */
/* Lighting                                                                  */
/* ========================================================================= */

#define LIGHT_STEPS 256u
#define LIGHT_MASK  255u

/*
 * Rotating light:
 *
 *     x = 640 cos(theta)
 *     y = 800
 *     z = 640 sin(theta)
 *
 * sqrt(640^2 + 800^2) ~= 1024, so the direction is already
 * essentially unit length in Q10.
 */
#define LIGHT_Y 800

#define AMBIENT_LIGHT 40u
#define DIRECT_LIGHT  215u

/* ========================================================================= */
/* Buffers and caches                                                        */
/* ========================================================================= */

static unsigned int framebuffer[2][PIXEL_COUNT];

/*
 * Camera-ray cache.
 */
static int ray_x[WIDTH];
static int ray_x_squared[WIDTH];

static int ray_y[HEIGHT];

/*
 * (ray_y^2 + ray_z^2) >> 10
 */
static int ray_y_base[HEIGHT];


/*
 * Visible sphere span for each row.
 *
 * row_count[y] == 0 means no sphere pixels on that row.
 */
static unsigned short row_start[HEIGHT];
static unsigned short row_count[HEIGHT];


/*
 * One signed Q7 normal per screen pixel.
 *
 * Only pixels inside the sphere spans are initialized/read.
 */
static signed char normal_x[PIXEL_COUNT];
static signed char normal_y[PIXEL_COUNT];
static signed char normal_z[PIXEL_COUNT];


/*
 * Indexed with:
 *
 *     cached_normal + 128
 *
 * Result is a Q10 contribution to N dot L.
 */
static int light_x_contribution[256];
static int light_y_contribution[256];
static int light_z_contribution[256];


/*
 * diffuse level 0..1024 -> packed grayscale Pixel32.
 */
static unsigned int shade_lut[1025];


/* ========================================================================= */
/* 256-step light sine                                                       */
/* ========================================================================= */

static short light_sin[LIGHT_STEPS] = {
       0,   16,   31,   47,   63,   78,   94,  109,
     125,  140,  156,  171,  186,  201,  216,  230,
     245,  259,  274,  288,  302,  315,  329,  342,
     356,  369,  381,  394,  406,  418,  430,  441,
     453,  464,  474,  485,  495,  505,  514,  523,
     532,  541,  549,  557,  564,  572,  579,  585,
     591,  597,  603,  608,  612,  617,  621,  624,
     628,  631,  633,  635,  637,  638,  639,  640,
     640,  640,  639,  638,  637,  635,  633,  631,
     628,  624,  621,  617,  612,  608,  603,  597,
     591,  585,  579,  572,  564,  557,  549,  541,
     532,  523,  514,  505,  495,  485,  474,  464,
     453,  441,  430,  418,  406,  394,  381,  369,
     356,  342,  329,  315,  302,  288,  274,  259,
     245,  230,  216,  201,  186,  171,  156,  140,
     125,  109,   94,   78,   63,   47,   31,   16,
       0,  -16,  -31,  -47,  -63,  -78,  -94, -109,
    -125, -140, -156, -171, -186, -201, -216, -230,
    -245, -259, -274, -288, -302, -315, -329, -342,
    -356, -369, -381, -394, -406, -418, -430, -441,
    -453, -464, -474, -485, -495, -505, -514, -523,
    -532, -541, -549, -557, -564, -572, -579, -585,
    -591, -597, -603, -608, -612, -617, -621, -624,
    -628, -631, -633, -635, -637, -638, -639, -640,
    -640, -640, -639, -638, -637, -635, -633, -631,
    -628, -624, -621, -617, -612, -608, -603, -597,
    -591, -585, -579, -572, -564, -557, -549, -541,
    -532, -523, -514, -505, -495, -485, -474, -464,
    -453, -441, -430, -418, -406, -394, -381, -369,
    -356, -342, -329, -315, -302, -288, -274, -259,
    -245, -230, -216, -201, -186, -171, -156, -140,
    -125, -109,  -94,  -78,  -63,  -47,  -31,  -16,
};


/* ========================================================================= */
/* Integer square root                                                       */
/* ========================================================================= */

static unsigned int isqrt(unsigned int value)
{
    unsigned int result;
    unsigned int bit;
    unsigned int trial;

    result = 0u;

    /*
     * Highest power of four representable in 32 bits.
     */
    bit = 1u << 30;

    while (bit > value)
        bit >>= 2;

    while (bit != 0u) {
        trial =
            result + bit;

        if (value >= trial) {
            value -=
                trial;

            result =
                (result >> 1)
                + bit;
        }
        else {
            result >>=
                1;
        }

        bit >>=
            2;
    }

    return result;
}


/* ========================================================================= */
/* Color                                                                     */
/* ========================================================================= */

static unsigned int pack_gray(unsigned int value)
{
    return
          (value << 24)
        | (value << 16)
        | (value << 8);
}


/* ========================================================================= */
/* Camera-ray cache                                                          */
/* ========================================================================= */

static void initialize_camera_rays(void)
{
    unsigned int x;
    unsigned int y;

    int value;


    x =
        0u;

    while (x < WIDTH) {
        value =
            -PLANE_HALF_X
            +
            (
                (2 * PLANE_HALF_X * (int)x)
                /
                (int)(WIDTH - 1u)
            );

        ray_x[x] =
            value;

        ray_x_squared[x] =
            (value * value)
            >> GEOM_SHIFT;

        x +=
            1u;
    }


    y =
        0u;

    while (y < HEIGHT) {
        /*
         * Positive Y is upward in world space.
         */
        value =
            PLANE_HALF_Y
            -
            (
                (2 * PLANE_HALF_Y * (int)y)
                /
                (int)(HEIGHT - 1u)
            );

        ray_y[y] =
            value;

        ray_y_base[y] =
            (
                value * value
                +
                RAY_Z * RAY_Z
            )
            >> GEOM_SHIFT;

        y +=
            1u;
    }
}


/* ========================================================================= */
/* Sphere scanline spans                                                     */
/* ========================================================================= */

/*
 * With:
 *
 *     C = (0,0,-3)
 *     R = 1
 *     D = (u,v,2)
 *
 * the ray/sphere discriminant is:
 *
 *     disc = 144 - 32*a
 *
 * where:
 *
 *     a = dot(D,D)
 *
 * The cached a is Q10. The expression below stores disc in Q20.
 */
static int sphere_discriminant(
    unsigned int x,
    unsigned int y
)
{
    int a;

    a =
        ray_x_squared[x]
        +
        ray_y_base[y];

    return
        150994944
        -
        (a << 15);
}


static void initialize_scanlines(void)
{
    unsigned int y;

    unsigned int left;
    unsigned int right;


    y =
        0u;

    while (y < HEIGHT) {
        left =
            0u;

        while (
            left < WIDTH
            &&
            sphere_discriminant(left, y) < 0
        ) {
            left +=
                1u;
        }


        if (left == WIDTH) {
            row_start[y] =
                0u;

            row_count[y] =
                0u;

            y +=
                1u;

            continue;
        }


        right =
            WIDTH - 1u;

        while (
            right > left
            &&
            sphere_discriminant(right, y) < 0
        ) {
            right -=
                1u;
        }


        row_start[y] =
            (unsigned short)left;

        row_count[y] =
            (unsigned short)(
                right - left + 1u
            );


        y +=
            1u;
    }
}


/* ========================================================================= */
/* Static sphere normals                                                     */
/* ========================================================================= */

static signed char quantize_normal(int value)
{
    int q;

    /*
     * Sphere radius is exactly GEOM_ONE, so hit coordinates are
     * already Q10 normal components.
     */
    q =
        value >> 3;

    /*
     * Avoid +128 wrapping to -128 in signed char.
     */
    if (q > 127)
        q = 127;

    if (q < -127)
        q = -127;

    return
        (signed char)q;
}


static void initialize_sphere_normals(void)
{
    unsigned int y;
    unsigned int x;

    unsigned int start;
    unsigned int count;
    unsigned int index;

    int dx;
    int dy;

    int a;

    int disc;
    unsigned int root;

    int t;

    int hit_x;
    int hit_y;
    int hit_z;


    y =
        0u;

    while (y < HEIGHT) {
        start =
            (unsigned int)row_start[y];

        count =
            (unsigned int)row_count[y];


        if (count == 0u) {
            y +=
                1u;

            continue;
        }


        x =
            start;

        index =
            y * WIDTH
            +
            start;


        while (x < start + count) {
            dx =
                ray_x[x];

            dy =
                ray_y[y];


            a =
                ray_x_squared[x]
                +
                ray_y_base[y];


            disc =
                150994944
                -
                (a << 15);


            if (disc < 0) {
                /*
                 * Should not occur inside a cached span, but keep the
                 * cache deterministic if integer rounding hits the edge.
                 */
                normal_x[index] =
                    0;

                normal_y[index] =
                    0;

                normal_z[index] =
                    0;
            }
            else {
                root =
                    isqrt(
                        (unsigned int)disc
                    );


                /*
                 * Nearest root.
                 *
                 * t is Q10.
                 *
                 * Real equation:
                 *
                 *     t = (12 - sqrt(144 - 32a)) / (2a)
                 */
                t =
                    (
                        (
                            12288
                            -
                            (int)root
                        )
                        << GEOM_SHIFT
                    )
                    /
                    (a << 1);


                hit_x =
                    (dx * t)
                    >> GEOM_SHIFT;

                hit_y =
                    (dy * t)
                    >> GEOM_SHIFT;

                hit_z =
                    CAMERA_Z
                    +
                    (
                        (RAY_Z * t)
                        >> GEOM_SHIFT
                    );


                normal_x[index] =
                    quantize_normal(
                        hit_x
                    );

                normal_y[index] =
                    quantize_normal(
                        hit_y
                    );

                normal_z[index] =
                    quantize_normal(
                        hit_z
                    );
            }


            x +=
                1u;

            index +=
                1u;
        }


        y +=
            1u;
    }
}


/* ========================================================================= */
/* Shading LUT                                                               */
/* ========================================================================= */

static void initialize_shade_lut(void)
{
    unsigned int level;
    unsigned int intensity;


    level =
        0u;

    while (level <= 1024u) {
        intensity =
            AMBIENT_LIGHT
            +
            (
                DIRECT_LIGHT
                * level
            )
            / 1024u;


        if (intensity > 255u)
            intensity = 255u;


        shade_lut[level] =
            pack_gray(
                intensity
            );


        level +=
            1u;
    }
}


/* ========================================================================= */
/* Light contribution LUTs                                                   */
/* ========================================================================= */

/*
 * Y never changes, so this table is initialized once.
 *
 * normal is Q7 and light is Q10:
 *
 *     Q7 * Q10 >> 7 = Q10
 */
static void initialize_y_contributions(void)
{
    unsigned int i;

    int accumulator;


    /*
     * (-128 * LIGHT_Y) without multiplication.
     */
    accumulator =
        -(LIGHT_Y << NORMAL_SHIFT);


    i =
        0u;

    while (i < 256u) {
        light_y_contribution[i] =
            accumulator
            >> NORMAL_SHIFT;

        accumulator +=
            LIGHT_Y;

        i +=
            1u;
    }
}


/*
 * X/Z change with the sun angle.
 *
 * Build both tables once per light step by repeated addition. The
 * sphere render loop then needs only table lookups and additions.
 */
static void initialize_light_contributions(unsigned int step)
{
    unsigned int i;

    int light_x;
    int light_z;

    int accumulator_x;
    int accumulator_z;


    light_x =
        (int)
        light_sin[
            (step + 64u)
            & LIGHT_MASK
        ];

    light_z =
        (int)
        light_sin[
            step
            & LIGHT_MASK
        ];


    accumulator_x =
        -(light_x << NORMAL_SHIFT);

    accumulator_z =
        -(light_z << NORMAL_SHIFT);


    i =
        0u;

    while (i < 256u) {
        light_x_contribution[i] =
            accumulator_x
            >> NORMAL_SHIFT;

        light_z_contribution[i] =
            accumulator_z
            >> NORMAL_SHIFT;


        accumulator_x +=
            light_x;

        accumulator_z +=
            light_z;


        i +=
            1u;
    }
}


/* ========================================================================= */
/* Render                                                                    */
/* ========================================================================= */

/*
 * Hot loop:
 *
 *     3 cached-normal loads
 *     3 contribution-table loads
 *     2 adds
 *     clamp
 *     1 shade-table load
 *     1 framebuffer store
 *
 * No multiply/divide/sqrt per sphere pixel.
 */
static void render_frame(unsigned int *buffer)
{
    unsigned int y;

    unsigned int start;
    unsigned int count;
    unsigned int index;
    unsigned int end;

    int nx;
    int ny;
    int nz;

    int diffuse;


    y =
        0u;

    while (y < HEIGHT) {
        start =
            (unsigned int)
            row_start[y];

        count =
            (unsigned int)
            row_count[y];


        if (count == 0u) {
            y +=
                1u;

            continue;
        }


        index =
            y * WIDTH
            +
            start;

        end =
            index
            +
            count;


        while (index < end) {
            nx =
                (int)normal_x[index]
                +
                NORMAL_BIAS;

            ny =
                (int)normal_y[index]
                +
                NORMAL_BIAS;

            nz =
                (int)normal_z[index]
                +
                NORMAL_BIAS;


            diffuse =
                  light_x_contribution[
                      (unsigned int)nx
                  ]
                + light_y_contribution[
                      (unsigned int)ny
                  ]
                + light_z_contribution[
                      (unsigned int)nz
                  ];


            if (diffuse < 0)
                diffuse = 0;
            else if (diffuse > GEOM_ONE)
                diffuse = GEOM_ONE;


            buffer[index] =
                shade_lut[
                    (unsigned int)diffuse
                ];


            index +=
                1u;
        }


        y +=
            1u;
    }
}


/* ========================================================================= */
/* Sun animation state                                                       */
/* ========================================================================= */

static unsigned int sun_step;


/* ========================================================================= */
/* Main                                                                      */
/* ========================================================================= */

int main(void)
{
    unsigned int back_buffer;


    /*
     * Static storage is zero-initialized, so framebuffer[0] is already black.
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
    /* One-time geometry/shading preprocessing                               */
    /* --------------------------------------------------------------------- */

    initialize_camera_rays();

    initialize_scanlines();

    initialize_sphere_normals();

    initialize_shade_lut();

    initialize_y_contributions();


    /* --------------------------------------------------------------------- */
    /* Initial frame                                                         */
    /* --------------------------------------------------------------------- */

    sun_step =
        0u;

    initialize_light_contributions(
        sun_step
    );


    back_buffer =
        1u;


    render_frame(
        framebuffer[back_buffer]
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
        sun_step =
            (sun_step + 1u)
            & LIGHT_MASK;

        initialize_light_contributions(
            sun_step
        );


        render_frame(
            framebuffer[back_buffer]
        );


        screen(
            1u,
            (unsigned int)framebuffer[back_buffer]
        );


        back_buffer ^=
            1u;
    }


    return 0;
}
