using Test
using Libdl
include(joinpath(@__DIR__, "../../examples/cap-set/evaluator/capset.jl"))
using .CapSetEvaluator: greedy_cap, is_cap

@testset "Independent exact cap checker" begin
    @test is_cap([[0, 0], [1, 0]], 2)
    @test !is_cap([[0, 0], [0, 0]], 2)
    @test !is_cap([[0, 0], [1, 0], [2, 0]], 2)
    @test !is_cap([[0], [1, 0]], 2)
    @test !is_cap([[0, 3]], 2)
end

@testset "Lexicographic greedy reference" begin
    handle = dlopen(joinpath(@__DIR__, "../../build/examples/seed.so"))
    try
        fptr = dlsym(handle, :priority)
        for n in 1:6
            cap = greedy_cap(fptr, n)
            @test length(cap) == 2^n
            @test is_cap(cap, n)
            if n <= 4
                # Independent enumeration and naive trial-addition checker.
                points = sort!(vec([collect(point) for point in Iterators.product(ntuple(_ -> 0:2, n)...)]); by=Tuple)
                reference = Vector{Vector{Int}}()
                for point in points
                    trial = [reference; [point]]
                    is_cap(trial, n) && push!(reference, point)
                end
                @test cap == reference
            end
        end
    finally
        dlclose(handle)
    end
end
