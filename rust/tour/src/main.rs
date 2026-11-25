//! HEXUDON — bộ giải tour trong ngày bằng LNS (25/11/2026).
//!
//! Bài toán: team orienteering có phủ chuỗi. Mỗi xe tuần tra đi một dãy spot từ ô xuất phát,
//! tổng số bước ≤ số bước của ngày (và tổng xăng ≤ xăng của xe nếu có chặn). Mỗi xe thu tối đa
//! một phần ở mỗi spot; mỗi spot nhận tối đa `kho` lượt thu. Tối đa theo thứ tự từ điển:
//! (chuỗi mới cả trận, số chuỗi trong ngày, số phần), hoà thì tổng số bước nhỏ hơn.
//!
//! Vào (stdin, các số cách nhau bởi khoảng trắng):
//!   V N mili_giây seed chặn_xăng(0/1)
//!   T                                   số bước của ngày
//!   fuel[V]                             xăng hiện có của từng xe
//!   start_spot[V]                       spot trùng ô xuất phát của xe (-1 nếu không)
//!   brand[N] stock[N] new[N]            chuỗi, kho, 1 nếu chuỗi chưa thu trong trận
//!   cost[(V+N) x N]                     số bước từ nút i (V ô xuất phát rồi N spot) tới spot j
//!   fuel_cost[(V+N) x N]                (chỉ khi chặn_xăng = 1)
//! Ra (stdout): V dòng, mỗi dòng "k s1 s2 ... sk" là tour của một xe.
//!
//! Chỉ dùng thư viện chuẩn: không tải crate, không cần mạng.

use std::io::{self, Read, Write};
use std::time::{Duration, Instant};

const INF: i64 = i64::MAX / 4;

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // xorshift64*
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % (n as u64)) as usize
    }
}

struct Problem {
    v: usize,
    n: usize,
    t: i64,
    cap_fuel: bool,
    fuel: Vec<i64>,
    start_spot: Vec<i64>,
    brand: Vec<usize>,
    stock: Vec<i64>,
    is_new: Vec<bool>,
    n_brands: usize,
    cost: Vec<i64>,      // (V+N) x N
    fcost: Vec<i64>,     // (V+N) x N
}

impl Problem {
    #[inline]
    fn c(&self, from: usize, to: usize) -> i64 {
        self.cost[from * self.n + to]
    }
    #[inline]
    fn f(&self, from: usize, to: usize) -> i64 {
        self.fcost[from * self.n + to]
    }
    /// Nút của spot k trong ma trận (sau V ô xuất phát).
    #[inline]
    fn node(&self, k: usize) -> usize {
        self.v + k
    }
}

#[derive(Clone)]
struct Sol {
    tours: Vec<Vec<usize>>,
    len: Vec<i64>,
    fuel: Vec<i64>,
    visits: Vec<i64>,        // số xe ghé mỗi spot (kể cả spot ở ô xuất phát)
    brand_cnt: Vec<i64>,
    has: Vec<Vec<bool>>,     // has[v][k]
}

fn tour_cost(p: &Problem, v: usize, tour: &[usize]) -> (i64, i64) {
    let mut prev = v;
    let (mut s, mut f) = (0i64, 0i64);
    for &k in tour {
        s += p.c(prev, k);
        if p.cap_fuel {
            f += p.f(prev, k);
        }
        prev = p.node(k);
    }
    (s, f)
}

impl Sol {
    fn empty(p: &Problem) -> Sol {
        let mut s = Sol {
            tours: vec![Vec::new(); p.v],
            len: vec![0; p.v],
            fuel: vec![0; p.v],
            visits: vec![0; p.n],
            brand_cnt: vec![0; p.n_brands],
            has: vec![vec![false; p.n]; p.v],
        };
        for v in 0..p.v {
            if p.start_spot[v] >= 0 {
                let k = p.start_spot[v] as usize;
                s.visits[k] += 1;
                s.brand_cnt[p.brand[k]] += 1;
                s.has[v][k] = true;
            }
        }
        s
    }

    fn key(&self, p: &Problem) -> (i64, i64, i64, i64) {
        let mut new = 0;
        let mut day = 0;
        for b in 0..p.n_brands {
            if self.brand_cnt[b] > 0 {
                day += 1;
            }
        }
        // chuỗi mới: chuỗi có trong ngày mà chưa thu trong trận (is_new theo chuỗi)
        let mut seen = vec![false; p.n_brands];
        for k in 0..p.n {
            let b = p.brand[k];
            if p.is_new[k] && self.brand_cnt[b] > 0 && !seen[b] {
                seen[b] = true;
                new += 1;
            }
        }
        let q: i64 = self.visits.iter().sum();
        let total: i64 = self.len.iter().sum();
        (new, day, q, -total)
    }

    fn insert(&mut self, p: &Problem, v: usize, pos: usize, k: usize) {
        self.tours[v].insert(pos, k);
        let (s, f) = tour_cost(p, v, &self.tours[v]);
        self.len[v] = s;
        self.fuel[v] = f;
        self.visits[k] += 1;
        self.brand_cnt[p.brand[k]] += 1;
        self.has[v][k] = true;
    }

    fn remove(&mut self, p: &Problem, v: usize, pos: usize) {
        let k = self.tours[v].remove(pos);
        let (s, f) = tour_cost(p, v, &self.tours[v]);
        self.len[v] = s;
        self.fuel[v] = f;
        self.visits[k] -= 1;
        self.brand_cnt[p.brand[k]] -= 1;
        self.has[v][k] = false;
    }
}

/// Chi phí (bước, xăng) tăng thêm khi chèn spot k vào tour của xe v ở vị trí pos.
#[inline]
fn delta(p: &Problem, sol: &Sol, v: usize, pos: usize, k: usize) -> (i64, i64) {
    let tour = &sol.tours[v];
    let prev = if pos == 0 { v } else { p.node(tour[pos - 1]) };
    let a = p.c(prev, k);
    if a >= INF {
        return (INF, INF);
    }
    let fa = if p.cap_fuel { p.f(prev, k) } else { 0 };
    if pos < tour.len() {
        let next = tour[pos];
        let b = p.c(p.node(k), next);
        if b >= INF {
            return (INF, INF);
        }
        let old = p.c(prev, next);
        let (fb, fold) = if p.cap_fuel { (p.f(p.node(k), next), p.f(prev, next)) } else { (0, 0) };
        (a + b - old, fa + fb - fold)
    } else {
        (a, fa)
    }
}

/// Chèn tham lam tới khi không chèn được nữa: ưu tiên chuỗi mới, rồi chuỗi chưa có trong ngày,
/// rồi chi phí chèn nhỏ nhất. Nhiễu nhỏ trên chi phí để mỗi lần sửa một khác.
fn repair(p: &Problem, sol: &mut Sol, rng: &mut Rng, noise: i64) {
    loop {
        let mut best: Option<(i64, i64, i64, usize, usize, usize)> = None; // (ưu tiên, -Δ, ..)
        // noise > 0: mỗi lượt chèn bỏ qua ngẫu nhiên một phần ứng viên (chèn ngẫu nhiên có chọn lọc)
        let skip = if noise > 0 { 1 + (rng.next() % 3) } else { 0 };
        for k in 0..p.n {
            if sol.visits[k] >= p.stock[k] {
                continue;
            }
            let b = p.brand[k];
            let pri = if sol.brand_cnt[b] == 0 { if p.is_new[k] { 2 } else { 1 } } else { 0 };
            if skip > 0 && rng.next() % 10 < skip {
                continue;
            }
            for v in 0..p.v {
                if sol.has[v][k] {
                    continue;
                }
                let tour_len = sol.tours[v].len();
                for pos in 0..=tour_len {
                    let (ds, df) = delta(p, sol, v, pos, k);
                    if ds >= INF || sol.len[v] + ds > p.t {
                        continue;
                    }
                    if p.cap_fuel && sol.fuel[v] + df > p.fuel[v] {
                        continue;
                    }
                    let jitter = if noise > 0 { (rng.next() % (noise as u64 + 1)) as i64 } else { 0 };
                    let cand = (pri, -(ds + jitter), 0, v, pos, k);
                    if best.map_or(true, |bb| (cand.0, cand.1) > (bb.0, bb.1)) {
                        best = Some(cand);
                    }
                }
            }
        }
        match best {
            Some((_, _, _, v, pos, k)) => sol.insert(p, v, pos, k),
            None => {
                if noise > 0 {
                    // các ứng viên bị bỏ qua có thể còn chèn được: một lượt cuối không nhiễu
                    repair(p, sol, rng, 0);
                }
                return;
            }
        }
    }
}

/// 2-opt và or-opt trong từng tour: rút ngắn số bước (giữ nguyên tập spot) để chừa chỗ chèn.
fn improve(p: &Problem, sol: &mut Sol) {
    for v in 0..p.v {
        let mut improved = true;
        while improved {
            improved = false;
            let n = sol.tours[v].len();
            if n < 2 {
                break;
            }
            // 2-opt: đảo đoạn [i, j]
            'outer: for i in 0..n - 1 {
                for j in i + 1..n {
                    let mut t = sol.tours[v].clone();
                    t[i..=j].reverse();
                    let (s, f) = tour_cost(p, v, &t);
                    if s < sol.len[v] && (!p.cap_fuel || f <= p.fuel[v]) {
                        sol.tours[v] = t;
                        sol.len[v] = s;
                        sol.fuel[v] = f;
                        improved = true;
                        break 'outer;
                    }
                }
            }
            if improved {
                continue;
            }
            // or-opt: chuyển một spot sang vị trí khác
            let n = sol.tours[v].len();
            'outer2: for i in 0..n {
                for j in 0..n {
                    if i == j {
                        continue;
                    }
                    let mut t = sol.tours[v].clone();
                    let k = t.remove(i);
                    t.insert(j, k);
                    let (s, f) = tour_cost(p, v, &t);
                    if s < sol.len[v] && (!p.cap_fuel || f <= p.fuel[v]) {
                        sol.tours[v] = t;
                        sol.len[v] = s;
                        sol.fuel[v] = f;
                        improved = true;
                        break 'outer2;
                    }
                }
            }
        }
    }
}

fn destroy(p: &Problem, sol: &mut Sol, rng: &mut Rng) {
    let total: usize = sol.tours.iter().map(|t| t.len()).sum();
    if total == 0 {
        return;
    }
    let r = if rng.below(10) == 0 { 1 + total / 2 } else { 1 + rng.below(6.min(total).max(1)) };
    for _ in 0..r {
        let nonempty: Vec<usize> = (0..p.v).filter(|&v| !sol.tours[v].is_empty()).collect();
        if nonempty.is_empty() {
            break;
        }
        let v = nonempty[rng.below(nonempty.len())];
        if rng.below(3) == 0 {
            // bỏ một đoạn liền nhau
            let n = sol.tours[v].len();
            let start = rng.below(n);
            let cnt = 1 + rng.below((n - start).min(3));
            for _ in 0..cnt {
                sol.remove(p, v, start);
            }
        } else {
            let pos = rng.below(sol.tours[v].len());
            sol.remove(p, v, pos);
        }
    }
}

fn parse(input: &str) -> Problem {
    let mut it = input.split_ascii_whitespace().map(|x| x.parse::<i64>().expect("số không hợp lệ"));
    let mut next = || it.next().expect("thiếu số");
    let v = next() as usize;
    let n = next() as usize;
    let _ms = next();
    let _seed = next();
    let cap_fuel = next() != 0;
    let t = next();
    let fuel = (0..v).map(|_| next()).collect();
    let start_spot = (0..v).map(|_| next()).collect();
    let brand: Vec<usize> = (0..n).map(|_| next() as usize).collect();
    let stock = (0..n).map(|_| next()).collect();
    let is_new = (0..n).map(|_| next() != 0).collect();
    let cost: Vec<i64> = (0..(v + n) * n).map(|_| next()).collect();
    let fcost = if cap_fuel { (0..(v + n) * n).map(|_| next()).collect() } else { vec![0; (v + n) * n] };
    let n_brands = brand.iter().copied().max().map_or(0, |m| m + 1);
    Problem { v, n, t, cap_fuel, fuel, start_spot, brand, stock, is_new, n_brands, cost, fcost }
}

fn header(input: &str) -> (u64, u64) {
    let mut it = input.split_ascii_whitespace();
    it.next();
    it.next();
    let ms: u64 = it.next().and_then(|x| x.parse().ok()).unwrap_or(1000);
    let seed: u64 = it.next().and_then(|x| x.parse().ok()).unwrap_or(1);
    (ms, seed)
}

fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).expect("không đọc được stdin");
    let started = Instant::now();
    let (ms, seed) = header(&input);
    let p = parse(&input);
    let limit = Duration::from_millis(ms);
    let mut rng = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1);

    let mut cur = Sol::empty(&p);
    repair(&p, &mut cur, &mut rng, 0);
    improve(&p, &mut cur);
    repair(&p, &mut cur, &mut rng, 0);
    let mut best = cur.clone();
    let mut best_key = best.key(&p);
    let mut cur_key = best_key;
    let mut iters: u64 = 0;
    let mut stale: u64 = 0;
    while started.elapsed() < limit {
        iters += 1;
        stale += 1;
        let frac = started.elapsed().as_secs_f64() / limit.as_secs_f64().max(1e-9);
        let temp = 1.0 * (1.0 - frac) + 0.05;
        let mut cand = cur.clone();
        destroy(&p, &mut cand, &mut rng);
        improve(&p, &mut cand);
        repair(&p, &mut cand, &mut rng, 2);
        improve(&p, &mut cand);
        repair(&p, &mut cand, &mut rng, 0);
        let k = cand.key(&p);
        // nhận nếu không kém về (L, D, Q); thỉnh thoảng nhận kém hơn một phần để thoát cực trị
        let better = (k.0, k.1, k.2) >= (cur_key.0, cur_key.1, cur_key.2);
        let worse_by = cur_key.2 - k.2;
        let sa = (k.0, k.1) == (cur_key.0, cur_key.1) && worse_by > 0
            && ((rng.next() % 1_000_000) as f64) < 1_000_000.0 * (-(worse_by as f64) / temp).exp();
        if better || sa {
            cur = cand;
            cur_key = k;
            if k > best_key {
                best = cur.clone();
                best_key = k;
                stale = 0;
            }
        }
        if stale > 300 {
            // kẹt: quay về bài tốt nhất rồi phá lớn
            cur = best.clone();
            for _ in 0..3 {
                destroy(&p, &mut cur, &mut rng);
            }
            repair(&p, &mut cur, &mut rng, 2);
            improve(&p, &mut cur);
            repair(&p, &mut cur, &mut rng, 0);
            cur_key = cur.key(&p);
            stale = 0;
        }
    }
    let out = io::stdout();
    let mut out = out.lock();
    for v in 0..p.v {
        let t = &best.tours[v];
        let mut line = t.len().to_string();
        for k in t {
            line.push(' ');
            line.push_str(&k.to_string());
        }
        writeln!(out, "{}", line).unwrap();
    }
    eprintln!("tour: {} vòng, L{} D{} Q{} bước {}", iters, best_key.0, best_key.1, best_key.2, -best_key.3);
}
