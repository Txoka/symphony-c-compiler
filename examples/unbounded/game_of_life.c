#include <symphony.h>

/*
 * Optimized Conway's Game of Life for Dynphony/Symphony.
 *
 * Main optimizations:
 *
 *   - The two Pixel32 framebuffers ARE the two Life generations.
 *     There is no separate simulation buffer and no render/copy pass.
 *
 *   - Interior cells use a horizontal sliding 3-column window.
 *     After the first cell of a row, only 3 new source pixels are read
 *     per cell instead of 8.
 *
 *   - Toroidal wrapping is kept completely out of the interior hot loop.
 *     The left/right columns and top/bottom rows are handled separately.
 *
 *   - A destination pixel is written only when its state differs from the
 *     same pixel already present in that back buffer.
 *
 *   - The interior hot loop uses row-local pointers and has no per-cell
 *     toroidal logic, multiplication, function calls, or lookahead branch.
 *
 * The last point is useful because double buffering means the back buffer
 * contains generation N-1 while we are producing generation N+1.  Therefore
 * it is safe to leave a pixel untouched exactly when its new state equals
 * the state already stored there.
 */

#define SCREEN_SETTING 32u

#define WIDTH       (4u * (SCREEN_SETTING + 1u))
#define HEIGHT      (3u * (SCREEN_SETTING + 1u))
#define PIXEL_COUNT (WIDTH * HEIGHT)

#define DEAD_PIXEL 0x00000000u
#define LIVE_PIXEL 0xFFFFFFFFu

static unsigned int framebuffer[2][PIXEL_COUNT];

static unsigned int rng_state;


/* ========================================================================= */
/* Random initialization                                                     */
/* ========================================================================= */

static unsigned int random_u32(void)
{
    unsigned int x;

    x = rng_state;

    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;

    rng_state = x;

    return x;
}


static void initialize_world(unsigned int *buffer)
{
    unsigned int i;

    i = 0u;

    while (i < PIXEL_COUNT) {
        /*
         * About 31.25% alive.
         */
        if ((random_u32() & 31u) < 10u)
            buffer[i] = LIVE_PIXEL;
        else
            buffer[i] = DEAD_PIXEL;

        i += 1u;
    }
}


/* ========================================================================= */
/* Cell helpers                                                              */
/* ========================================================================= */

/*
 * LIVE_PIXEL is all ones and DEAD_PIXEL is zero.
 *
 * Comparing against zero gives us an integer 0/1 state.  Keep this helper
 * tiny; the compiler should inline it, but the expression is also used
 * directly in the hottest loops below to avoid depending on inlining.
 */
static unsigned int cell_alive(unsigned int pixel)
{
    return pixel != 0u;
}


/*
 * Store only if the new state differs from what is already in the back
 * framebuffer.
 *
 * Because buffers alternate:
 *
 *     front = generation N
 *     back  = generation N-1
 *
 * while computing generation N+1.
 *
 * If N+1 at this position equals N-1, the back buffer already contains the
 * correct final pixel and no write is necessary.
 */
static void store_cell(
    unsigned int *destination,
    unsigned int index,
    unsigned int alive
)
{
    unsigned int pixel;

    if (alive)
        pixel = LIVE_PIXEL;
    else
        pixel = DEAD_PIXEL;

    if (destination[index] != pixel)
        destination[index] = pixel;
}


/* ========================================================================= */
/* Interior                                                                  */
/* ========================================================================= */

/*
 * Process x = 1 .. WIDTH-2 and y = 1 .. HEIGHT-2.
 *
 * For a given x, maintain sums of the three vertical columns:
 *
 *     left    center    right
 *
 *       a        b        c
 *       d        X        e
 *       f        g        h
 *
 * neighbors = left + center + right - X
 *
 * Moving one pixel right turns:
 *
 *     left   <- center
 *     center <- right
 *
 * and only the three pixels of the new right column need to be loaded.
 */
static void step_interior(
    unsigned int *source,
    unsigned int *destination
)
{
    unsigned int y;

    unsigned int prev_index;
    unsigned int row_index;
    unsigned int next_index;

    unsigned int *prev;
    unsigned int *row;
    unsigned int *next;
    unsigned int *dst;

    unsigned int x;

    unsigned int left_sum;
    unsigned int center_sum;
    unsigned int right_sum;

    unsigned int center_alive;
    unsigned int neighbors;
    unsigned int new_alive;
    unsigned int pixel;


    y = 1u;

    prev_index = 0u;
    row_index = WIDTH;
    next_index = WIDTH + WIDTH;


    while (y + 1u < HEIGHT) {
        prev = source + prev_index;
        row = source + row_index;
        next = source + next_index;
        dst = destination + row_index;


        left_sum =
              (prev[0] != 0u)
            + (row[0]  != 0u)
            + (next[0] != 0u);

        center_sum =
              (prev[1] != 0u)
            + (row[1]  != 0u)
            + (next[1] != 0u);

        right_sum =
              (prev[2] != 0u)
            + (row[2]  != 0u)
            + (next[2] != 0u);


        x = 1u;


        /*
         * All interior cells except the last one.
         *
         * The pointers are row-local, so the hot loop avoids expressions such
         * as source[prev + x + 2].  It also has no per-cell "do I need another
         * right column?" branch.
         */
        while (x + 2u < WIDTH) {
            center_alive =
                row[x] != 0u;

            neighbors =
                  left_sum
                + center_sum
                + right_sum
                - center_alive;


            /*
             * Life rule written so the common dead-cell path does not require
             * a nested if/else:
             *
             *   birth:    n == 3
             *   survival: alive && n == 2
             */
            new_alive =
                (neighbors == 3u)
                ||
                (
                    center_alive
                    &&
                    neighbors == 2u
                );


            if (new_alive)
                pixel = LIVE_PIXEL;
            else
                pixel = DEAD_PIXEL;

            if (dst[x] != pixel)
                dst[x] = pixel;


            left_sum = center_sum;
            center_sum = right_sum;

            right_sum =
                  (prev[x + 2u] != 0u)
                + (row[x + 2u]  != 0u)
                + (next[x + 2u] != 0u);


            x += 1u;
        }


        /*
         * Last interior cell.  No lookahead is needed.
         */
        center_alive =
            row[x] != 0u;

        neighbors =
              left_sum
            + center_sum
            + right_sum
            - center_alive;

        new_alive =
            (neighbors == 3u)
            ||
            (
                center_alive
                &&
                neighbors == 2u
            );

        if (new_alive)
            pixel = LIVE_PIXEL;
        else
            pixel = DEAD_PIXEL;

        if (dst[x] != pixel)
            dst[x] = pixel;


        y += 1u;

        prev_index += WIDTH;
        row_index += WIDTH;
        next_index += WIDTH;
    }
}

/* ========================================================================= */
/* Toroidal borders                                                          */
/* ========================================================================= */

/*
 * Border cells are a tiny fraction of the board, so clarity wins here.
 * This routine is deliberately not used by the interior hot loop.
 */
static unsigned int wrapped_neighbor_count(
    unsigned int *source,
    unsigned int x,
    unsigned int y
)
{
    unsigned int xm1;
    unsigned int xp1;
    unsigned int ym1;
    unsigned int yp1;

    unsigned int row0;
    unsigned int row1;
    unsigned int row2;

    xm1 =
        (x == 0u)
        ? WIDTH - 1u
        : x - 1u;

    xp1 =
        (x + 1u == WIDTH)
        ? 0u
        : x + 1u;

    ym1 =
        (y == 0u)
        ? HEIGHT - 1u
        : y - 1u;

    yp1 =
        (y + 1u == HEIGHT)
        ? 0u
        : y + 1u;


    row0 = ym1 * WIDTH;
    row1 = y   * WIDTH;
    row2 = yp1 * WIDTH;


    return
          cell_alive(source[row0 + xm1])
        + cell_alive(source[row0 + x])
        + cell_alive(source[row0 + xp1])

        + cell_alive(source[row1 + xm1])
        + cell_alive(source[row1 + xp1])

        + cell_alive(source[row2 + xm1])
        + cell_alive(source[row2 + x])
        + cell_alive(source[row2 + xp1]);
}


static void step_border_cell(
    unsigned int *source,
    unsigned int *destination,
    unsigned int x,
    unsigned int y
)
{
    unsigned int index;
    unsigned int alive;
    unsigned int neighbors;
    unsigned int new_alive;

    index =
          y * WIDTH
        + x;

    alive =
        source[index] != 0u;

    neighbors =
        wrapped_neighbor_count(
            source,
            x,
            y
        );


    if (alive) {
        new_alive =
            neighbors == 2u
            ||
            neighbors == 3u;
    }
    else {
        new_alive =
            neighbors == 3u;
    }


    store_cell(
        destination,
        index,
        new_alive
    );
}


static void step_borders(
    unsigned int *source,
    unsigned int *destination
)
{
    unsigned int x;
    unsigned int y;

    /*
     * Complete top and bottom rows, including corners.
     */
    x = 0u;

    while (x < WIDTH) {
        step_border_cell(
            source,
            destination,
            x,
            0u
        );

        step_border_cell(
            source,
            destination,
            x,
            HEIGHT - 1u
        );

        x += 1u;
    }


    /*
     * Left/right columns excluding corners already handled above.
     */
    y = 1u;

    while (y + 1u < HEIGHT) {
        step_border_cell(
            source,
            destination,
            0u,
            y
        );

        step_border_cell(
            source,
            destination,
            WIDTH - 1u,
            y
        );

        y += 1u;
    }
}


/* ========================================================================= */
/* Generation                                                                */
/* ========================================================================= */

static void step_world(
    unsigned int *source,
    unsigned int *destination
)
{
    step_interior(
        source,
        destination
    );

    step_borders(
        source,
        destination
    );
}


/* ========================================================================= */
/* Main                                                                      */
/* ========================================================================= */

int main(void)
{
    unsigned int front_buffer;
    unsigned int back_buffer;
    unsigned int temporary;


    rng_state =
        time_low();

    if (rng_state == 0u)
        rng_state = 0xA341316Cu;


    front_buffer = 0u;
    back_buffer = 1u;


    initialize_world(
        framebuffer[front_buffer]
    );


    /*
     * The back buffer starts zeroed.  It intentionally does NOT need to be
     * initialized to match the front buffer: on the first generation every
     * live destination cell is written, while dead cells may safely remain
     * zero.
     */


    screen(
        1u,
        (unsigned int)framebuffer[front_buffer]
    );

    screen(
        2u,
        SCREEN_SETTING
    );

    screen(
        0u,
        3u
    );


    while (1) {
        step_world(
            framebuffer[front_buffer],
            framebuffer[back_buffer]
        );


        screen(
            1u,
            (unsigned int)framebuffer[back_buffer]
        );


        temporary = front_buffer;
        front_buffer = back_buffer;
        back_buffer = temporary;
    }


    return 0;
}
