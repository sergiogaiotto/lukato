# CAs adicionais para o build da imagem

Solte aqui qualquer certificado `*.crt` (formato PEM) que o build precise confiar.
O estagio `builder` do Dockerfile acrescenta o que encontrar ao conjunto de CAs
usado pelo Python antes de baixar qualquer coisa.

Serve a um caso concreto: **proxy corporativo que faz interceptacao TLS**. Nesse
tipo de rede o proxy reassina os certificados com uma CA propria, e qualquer
download HTTPS de dentro do build falha com

    CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain

Exporte a CA do proxy de voces, coloque o arquivo aqui, e o build passa.

O diretorio vazio nao custa nada: sem `.crt`, o passo nao faz nada.

**Nao ponha chave privada aqui.** Isto e para certificado publico de CA, que e
material publico por definicao — e o diretorio e versionado.
