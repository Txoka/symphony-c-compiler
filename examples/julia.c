#include <symphony.h>

/*
 * Fast static Julia renderer for Dynphony/Symphony.
 *
 * c = -0.8 + 0.15625i
 *
 * Optimizations:
 *   - 1024x768 resolution (SCREEN_SETTING = 255)
 *   - Q10 fixed point: products fit signed 32-bit
 *   - static image: render once, then return (compiler emits halt)
 *   - x coordinates precomputed once
 *   - y coordinate calculated once per scanline, never per pixel
 *   - no division in the iteration hot loop
 *   - iteration body fully inlined
 *   - framebuffer pointer increments
 *   - static palette
 *
 * Every quadratic Julia set has 180-degree point symmetry because
 * f(-z) == f(z), so only half the pixels need fractal evaluation.
 */

#define SCREEN_SETTING 255u

#define WIDTH       (4u * (SCREEN_SETTING + 1u))
#define HEIGHT      (3u * (SCREEN_SETTING + 1u))
#define PIXEL_COUNT (WIDTH * HEIGHT)

#define FIX_SHIFT 10
#define FIX_ONE   (1 << FIX_SHIFT)

#define MAX_ITER 64u

#define X_MIN (-1418)  /* -1.385 Q10 */
#define X_MAX (1418)
#define Y_MIN (-1063)  /* -1.038 Q10 */
#define Y_MAX (1063)

#define ESCAPE2 (4 * FIX_ONE)

/*
 * c = -0.8 + 0.15625i in Q10.
 */
#define JULIA_CX (-819)
#define JULIA_CY (160)

static unsigned int framebuffer[PIXEL_COUNT];
static int x_coord[WIDTH];
static int y_coord[HEIGHT];
static unsigned int palette[MAX_ITER + 1u];


static unsigned int pack_rgb(
    unsigned int r,
    unsigned int g,
    unsigned int b
)
{
    return
          (r << 24)
        | (g << 16)
        | (b << 8);
}


static void initialize_palette(void)
{
    unsigned int i;
    unsigned int intensity;

    i = 0u;

    while (i < MAX_ITER) {
        intensity =
            (255u * i)
            /
            MAX_ITER;

        palette[i] =
            pack_rgb(
                intensity,
                (intensity * intensity) >> 8,
                255u - (intensity >> 1)
            );

        i += 1u;
    }

    palette[MAX_ITER] =
        0x00000000u;
}


static void initialize_coordinates(void)
{
    unsigned int x;
    unsigned int y;

    x = 0u;

    while (x < WIDTH) {
        x_coord[x] =
            X_MIN
            +
            (
                (X_MAX - X_MIN)
                * (int)x
            )
            /
            (int)(WIDTH - 1u);

        x += 1u;
    }


    y = 0u;

    while (y < HEIGHT) {
        y_coord[y] =
            Y_MIN
            +
            (
                (Y_MAX - Y_MIN)
                * (int)y
            )
            /
            (int)(HEIGHT - 1u);

        y += 1u;
    }
}


/*
 * Hot Julia iteration.
 *
 * The square terms are carried between iterations so each iteration performs
 * exactly three multiplications:
 *
 *     zx*zy
 *     new_zx*new_zx
 *     new_zy*new_zy
 */
static unsigned int iterate_pixel(
    int zx,
    int zy
)
{
    int zx2;
    int zy2;
    int zxy;

    unsigned int iteration;

    zx2 =
        (zx * zx)
        >> FIX_SHIFT;

    zy2 =
        (zy * zy)
        >> FIX_SHIFT;

    iteration = 0u;

    while (
        iteration < MAX_ITER
        &&
        zx2 + zy2 <= ESCAPE2
    ) {
        zxy =
            (zx * zy)
            >> FIX_SHIFT;

        zy =
            (zxy << 1)
            + JULIA_CY;

        zx =
            zx2
            - zy2
            + JULIA_CX;

        zx2 =
            (zx * zx)
            >> FIX_SHIFT;

        zy2 =
            (zy * zy)
            >> FIX_SHIFT;

        iteration += 1u;
    }

    return iteration;
}


static void render(void)
{
    unsigned int total;
    unsigned int half;
    unsigned int i;
    unsigned int x;
    unsigned int y;
    unsigned int mirror;
    int zx;
    int zy;
    unsigned int iteration;
    unsigned int color;

    /*
     * Universal quadratic-Julia point symmetry:
     *
     *     f(-z) = (-z)^2 + c = z^2 + c = f(z)
     *
     * With this origin-centered viewport, linear pixel i is paired with
     * PIXEL_COUNT - 1 - i.  Evaluate only half the image.
     */
    total = PIXEL_COUNT;
    half = (total + 1u) / 2u;

    i = 0u;
    y = 0u;

    while (i < half) {
        zy = y_coord[y];
        x = 0u;

        while (x < WIDTH && i < half) {
            zx = x_coord[x];

            iteration = iterate_pixel(zx, zy);
            color = palette[iteration];

            framebuffer[i] = color;

            mirror = total - 1u - i;

            if (mirror != i)
                framebuffer[mirror] = color;

            i += 1u;
            x += 1u;
        }

        /*
         * Keep the visible slow scan: top and bottom grow toward the center.
         */
        screen(
            1u,
            (unsigned int)framebuffer
        );

        y += 1u;
    }
}

int main(void)
{
    screen(
        1u,
        (unsigned int)framebuffer
    );

    screen(
        2u,
        SCREEN_SETTING
    );

    screen(
        0u,
        3u
    );


    initialize_palette();
    initialize_coordinates();

    render();


    return 0;
}
