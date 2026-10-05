module CapSetEvaluator

using Dates

struct InvalidPriority <: Exception
    message::String
end
Base.showerror(io::IO, error::InvalidPriority) = print(io, error.message)

# Base-3 enumeration, with the first coordinate most significant, is exactly
# lexicographic order. The greedy blocker uses this index representation.
function vectors(n)
    [digits(Int8, i; base=3, pad=n) |> reverse for i in 0:3^n-1]
end

function greedy_cap(fptr::Ptr{Cvoid}, n)
    points = vectors(n)
    priorities = Vector{Float64}(undef, length(points))
    for (i, v) in enumerate(points)
        input = copy(v)
        value = ccall(fptr, Cdouble, (Ptr{Int8}, Int32), input, n)
        input == v || throw(InvalidPriority("priority modified its input at n=$n"))
        isfinite(value) || throw(InvalidPriority("priority returned NaN/Inf at n=$n, vector=$(Tuple(v))"))
        priorities[i] = value
    end
    order = sortperm(eachindex(points); by=i -> (-priorities[i], i))
    blocked = falses(length(points))
    cap = Vector{Vector{Int8}}()
    for i in order
        blocked[i] && continue
        v = points[i]
        for x in cap
            # Any completion -(x+v) is forbidden from now on.
            z = 0
            for j in 1:n
                z = 3z + mod(-Int(x[j]) - Int(v[j]), 3)
            end
            blocked[z + 1] = true
        end
        push!(cap, v)
    end
    cap
end

"""Independent exact check: no construction state or candidate calls."""
function is_cap(cap, n)
    all(v -> length(v) == n && all(x -> x in (0, 1, 2), v), cap) || return false
    members = Set(Tuple(v) for v in cap)
    length(members) == length(cap) || return false
    for i in eachindex(cap), j in 1:i-1
        z = ntuple(k -> mod(-Int(cap[i][k]) - Int(cap[j][k]), 3), n)
        z in members && return false
    end
    true
end

function dump_cap(cap, n)
    directory = get(ENV, "FS_CAPSET_DUMP", "")
    isempty(directory) && return
    mkpath(directory)
    timestamp = Dates.format(now(UTC), dateformat"yyyymmddTHHMMSSsss")
    # PID and nanoseconds keep repeated scores and concurrent workers distinct.
    path = joinpath(directory, "$timestamp-$(getpid())-$(time_ns())-n$n.txt")
    open(path, "w") do file
        for v in cap
            println(file, join(v, ' '))
        end
    end
end

function score_instance(fptr::Ptr{Cvoid}, n)
    dimensions = n < 4 ? collect(1:n) : collect(4:min(n, 6))
    n > 6 && push!(dimensions, n)
    sizes = Int64[]
    for k in dimensions
        cap = greedy_cap(fptr, k)
        is_cap(cap, k) || return (Int32(1), Int64(0), sizes, "constructed set failed independent cap verification at n=$k")
        dump_cap(cap, k)
        push!(sizes, length(cap))
    end
    (Int32(0), sizes[end], sizes, "verified cap at n=$n")
end

end # module
