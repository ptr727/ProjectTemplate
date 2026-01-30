using System.Security.Cryptography;

namespace ptr727.ProjectTemplate.Benchmarks;

[MemoryDiagnoser]
[System.Diagnostics.CodeAnalysis.SuppressMessage(
    "Performance",
    "CA1515:Make types declared in an executable internal",
    Justification = "Benchmark classes must be public for BenchmarkDotNet"
)]
public class SampleBenchmark
{
    private const int N = 10000;
    private readonly byte[] _data;

    private readonly SHA256 _sha256 = SHA256.Create();

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Security",
        "CA5351:Do Not Use Broken Cryptographic Algorithms",
        Justification = "This is sample benchmark code."
    )]
    private readonly MD5 _md5 = MD5.Create();

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Security",
        "CA5394:Do not use insecure randomness",
        Justification = "This is sample benchmark code."
    )]
    public SampleBenchmark()
    {
        _data = new byte[N];
        new Random(42).NextBytes(_data);
    }

    [Benchmark]
    public byte[] Sha256() => _sha256.ComputeHash(_data);

    [Benchmark]
    public byte[] Md5() => _md5.ComputeHash(_data);
}
