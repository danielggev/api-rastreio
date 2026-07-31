/* ============================================
   GRUDADO EM VOCE - rastreio.js
   Consulta a API de rastreio e monta o resultado na pagina.

   Script CLASSICO, nao modulo: e carregado com `defer` pela secao.

   REGRA DE SEGURANCA: todo dado vindo da API entra com `textContent`, NUNCA
   com `innerHTML`. Os campos `rotulo` e `descricao` vem da Frete Rapido e das
   transportadoras -- se um dia qualquer um trouxer markup, `innerHTML` o
   executaria no navegador do cliente.
   ============================================ */

(function () {
  'use strict';

  /* Grupos em que o cliente precisa AGIR -- e onde esta pagina de fato evita
     um contato no suporte, entao recebem peso visual maior. */
  var EXIGEM_ACAO = ['aguardando_retirada', 'tentativa_falha'];

  /* O rotulo do status vem da propria API (sao 250+ codigos possiveis).
     O que definimos aqui e a orientacao: o que o cliente faz com aquilo. */
  var ORIENTACAO = {
    aguardando_retirada:
      'Sua encomenda chegou e esta esperando por voce no ponto de retirada. ' +
      'Se ninguem buscar dentro do prazo, ela volta pra gente.',
    tentativa_falha:
      'A transportadora tentou entregar e nao conseguiu. Normalmente uma nova ' +
      'tentativa acontece nos proximos dias uteis.',
    devolucao: 'Sua encomenda esta voltando para a nossa base.',
    sinistro: 'Houve um problema com sua encomenda durante o transporte.',
    cancelado: 'Este envio foi cancelado.',
    saiu_para_entrega: 'Sua encomenda saiu para entrega e deve chegar hoje!',
    entregue: 'Sua encomenda foi entregue. Esperamos que voce ame!',
    entrega_parcial: 'Parte do seu pedido foi entregue.'
  };

  /* ---------- Formatacao ---------- */

  function formatarDataHora(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    return d.toLocaleDateString('pt-BR', {
      day: '2-digit', month: '2-digit', year: 'numeric'
    }) + ' as ' + d.toLocaleTimeString('pt-BR', {
      hour: '2-digit', minute: '2-digit'
    });
  }

  /* Data pura ("2026-07-29"): montada na mao para o fuso nao deslocar o dia. */
  function formatarData(iso) {
    if (!iso) return null;
    var p = String(iso).split('-');
    return p.length === 3 ? p[2] + '/' + p[1] + '/' + p[0] : null;
  }

  /* ---------- Construcao do DOM ---------- */

  function el(tag, classe, texto) {
    var n = document.createElement(tag);
    if (classe) n.className = classe;
    if (texto !== undefined && texto !== null) n.textContent = texto;
    return n;
  }

  function linhaInfo(rotulo, valor) {
    if (!valor) return null;
    var li = el('div', 'rastreio-resultado__info');
    li.appendChild(el('span', 'rastreio-resultado__info-rotulo', rotulo));
    li.appendChild(el('span', 'rastreio-resultado__info-valor', valor));
    return li;
  }

  function montarSucesso(d) {
    var raiz = el('div', 'rastreio-resultado');
    var atual = d.status_atual;

    raiz.appendChild(el('p', 'rastreio-resultado__pedido', 'Pedido ' + d.pedido));

    var destaque = el(
      'div',
      'rastreio-resultado__status rastreio-resultado__status--' + atual.grupo
    );
    destaque.appendChild(el('strong', 'rastreio-resultado__status-rotulo', atual.rotulo));
    var quando = formatarDataHora(atual.data);
    if (quando) destaque.appendChild(el('span', 'rastreio-resultado__status-data', quando));
    raiz.appendChild(destaque);

    var orientacao = ORIENTACAO[atual.grupo];
    if (orientacao) {
      var classe = 'rastreio-resultado__aviso';
      if (EXIGEM_ACAO.indexOf(atual.grupo) !== -1) {
        classe += ' rastreio-resultado__aviso--acao';
      }
      raiz.appendChild(el('p', classe, orientacao));
    }

    if (d.entrega_atrasada) {
      raiz.appendChild(el(
        'p',
        'rastreio-resultado__aviso rastreio-resultado__aviso--atraso',
        'A previsao era ' + formatarData(d.previsao_entrega) +
        ' e a entrega esta atrasada. Ja estamos de olho.'
      ));
    }

    var infos = el('div', 'rastreio-resultado__infos');
    var transp = linhaInfo('Transportadora', d.transportadora);
    if (transp) infos.appendChild(transp);

    /* A previsao so interessa a quem AINDA espera. Omitimos em dois casos:
       - entregue: o pedido chegou, e a previsao vencida so apontaria um atraso
         para quem ja recebeu;
       - atrasado: o aviso acima ja menciona a data, repetir seria redundante. */
    var entregue = atual.grupo === 'entregue';
    if (!d.entrega_atrasada && !entregue) {
      var prev = linhaInfo('Previsao de entrega', formatarData(d.previsao_entrega));
      if (prev) infos.appendChild(prev);
    }
    if (infos.childElementCount) raiz.appendChild(infos);

    if (d.historico && d.historico.length) {
      raiz.appendChild(
        el('h2', 'rastreio-resultado__historico-titulo', 'Acompanhe o trajeto')
      );
      var lista = el('ol', 'rastreio-resultado__historico');
      d.historico.forEach(function (ev) {
        var item = el(
          'li',
          'rastreio-resultado__evento rastreio-resultado__evento--' + ev.grupo
        );
        item.appendChild(el('span', 'rastreio-resultado__evento-rotulo', ev.rotulo));
        var data = formatarDataHora(ev.data);
        if (data) item.appendChild(el('span', 'rastreio-resultado__evento-data', data));
        if (ev.descricao) {
          item.appendChild(el('span', 'rastreio-resultado__evento-descricao', ev.descricao));
        }
        lista.appendChild(item);
      });
      raiz.appendChild(lista);
    }

    return raiz;
  }

  function montarMensagem(d, modificador) {
    var raiz = el('div', 'rastreio-resultado rastreio-resultado--' + modificador);
    if (d.pedido) {
      raiz.appendChild(el('p', 'rastreio-resultado__pedido', 'Pedido ' + d.pedido));
    }
    raiz.appendChild(el('p', 'rastreio-resultado__mensagem', d.mensagem));
    return raiz;
  }

  function renderizar(alvo, resposta, dados) {
    alvo.replaceChildren();

    if (resposta.status === 422) {
      alvo.appendChild(montarMensagem(
        { mensagem: 'Confira o e-mail digitado: o formato parece invalido.' },
        'erro'
      ));
      return;
    }

    switch (dados.resultado) {
      case 'sucesso':         alvo.appendChild(montarSucesso(dados)); break;
      case 'sem_rastreio':    alvo.appendChild(montarMensagem(dados, 'aguardando')); break;
      case 'vazio_fr':        alvo.appendChild(montarMensagem(dados, 'indisponivel')); break;
      case 'nao_encontrado':  alvo.appendChild(montarMensagem(dados, 'nao-encontrado')); break;
      case 'limite_excedido': alvo.appendChild(montarMensagem(dados, 'limite')); break;
      default:
        alvo.appendChild(montarMensagem(
          { mensagem: dados.mensagem || 'Nao foi possivel consultar agora.' },
          'erro'
        ));
    }
  }

  /* ---------- Ligacao com o formulario ---------- */

  function iniciar() {
    var form = document.getElementById('rastreio-form');
    if (!form) return;

    var alvo = document.getElementById('rastreio-resultado');
    var botao = form.querySelector('.rastreio__submit');
    /* Endereco da API, definido pela secao Liquid. */
    var api = form.getAttribute('data-api');

    if (!alvo || !api) {
      console.error('[rastreio] falta #rastreio-resultado ou data-api no formulario.');
      return;
    }

    var emAndamento = false;

    form.addEventListener('submit', function (e) {
      e.preventDefault();

      if (!form.checkValidity()) { form.reportValidity(); return; }
      /* Duplo envio consumiria o limite de consultas do proprio cliente, que
         entao receberia um erro causado pela interface. */
      if (emAndamento) return;
      emAndamento = true;

      if (botao) botao.disabled = true;
      alvo.replaceChildren(
        el('p', 'rastreio-resultado__carregando', 'Procurando seu pedido...')
      );

      fetch(api, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: form.email.value.trim(),
          /* O "#" e tolerado pela API, mas limpar aqui evita ida e volta. */
          numero_pedido: form.pedido.value.trim().replace(/^#/, '')
        })
      })
        .then(function (resposta) {
          return resposta.json()
            .catch(function () { return {}; })
            .then(function (dados) { renderizar(alvo, resposta, dados); });
        })
        .catch(function () {
          alvo.replaceChildren(montarMensagem({
            mensagem: 'Nao conseguimos conectar. Verifique sua internet e tente de novo.'
          }, 'erro'));
        })
        .then(function () {
          emAndamento = false;
          if (botao) botao.disabled = false;
          /* No celular o resultado nasce fora da tela: sem isto o cliente
             clica, nada parece acontecer, e ele clica de novo. */
          if (window.matchMedia('(max-width: 1024px)').matches) {
            alvo.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          }
        });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', iniciar);
  } else {
    iniciar();
  }

  /* O editor de temas recarrega secoes sem recarregar a pagina. */
  document.addEventListener('shopify:section:load', iniciar);
})();
